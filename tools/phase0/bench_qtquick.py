"""Phase 0：Qt Quick 原型帧时间基准。

量三件事：
  1. 首帧耗时（引擎加载 → 第一帧）
  2. 匀速滚动 10 秒的帧时间分布（p50/p95/p99、掉帧数）
  3. 随机跳转 3 秒的最差帧（模拟快速拖动整表，压迫委托创建与图片解码）

用法：
    python tools/phase0/bench_qtquick.py                     # offscreen，无需窗口权限
    python tools/phase0/bench_qtquick.py --sync              # 关掉异步解码做对照
    python tools/phase0/bench_qtquick.py --visible           # 真实窗口 + vsync（真数字）
    python tools/phase0/bench_qtquick.py --data <目录> --records 500
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_DIR = os.path.join(tempfile.gettempdir(), "agnes_phase0")
HERE = Path(__file__).resolve().parent


def load_binding():
    """优先 PySide6（正式项目用），缺失时回退到本机已装的 PyQt5。"""
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtQml import QQmlApplicationEngine
        from PySide6.QtQuick import QQuickWindow

        return "PySide6", QTimer, QGuiApplication, QQmlApplicationEngine, QQuickWindow
    except ImportError:
        from PyQt5.QtCore import QTimer
        from PyQt5.QtGui import QGuiApplication
        from PyQt5.QtQml import QQmlApplicationEngine
        from PyQt5.QtQuick import QQuickWindow

        return "PyQt5", QTimer, QGuiApplication, QQmlApplicationEngine, QQuickWindow


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb() -> float:
    """当前进程工作集（Windows）；非 Windows 回退到 0 表示不可用。"""
    if not sys.platform.startswith("win"):
        return 0.0
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return counters.WorkingSetSize / 1024 / 1024 if ok else 0.0
    except Exception:
        return 0.0


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(p / 100 * len(ordered) + 0.5)) - 1))
    return ordered[idx]


def summarize(name: str, samples: list[float]) -> dict:
    if len(samples) < 2:
        return {"phase": name, "frames": len(samples)}
    deltas = [(samples[i] - samples[i - 1]) * 1000 for i in range(1, len(samples))]
    span = samples[-1] - samples[0]
    return {
        "phase": name,
        "frames": len(deltas),
        "duration_s": round(span, 2),
        "fps": round(len(deltas) / span, 1) if span else 0.0,
        "median_ms": round(statistics.median(deltas), 2),
        "mean_ms": round(statistics.fmean(deltas), 2),
        "p95_ms": round(percentile(deltas, 95), 2),
        "p99_ms": round(percentile(deltas, 99), 2),
        "max_ms": round(max(deltas), 2),
        "over_16ms": sum(1 for d in deltas if d > 16.7),
        "over_33ms": sum(1 for d in deltas if d > 33.3),
        "over_50ms": sum(1 for d in deltas if d > 50.0),
    }


def summarize_raw(name: str, values: list[float]) -> dict:
    """直接统计原始样本（用于 UI 线程阻塞探针：样本本身就是间隔毫秒）。"""
    if not values:
        return {"phase": name, "frames": 0}
    return {
        "phase": name,
        "frames": len(values),
        "median_ms": round(statistics.median(values), 2),
        "p95_ms": round(percentile(values, 95), 2),
        "p99_ms": round(percentile(values, 99), 2),
        "max_ms": round(max(values), 2),
        "over_50ms": sum(1 for v in values if v > 50.0),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Qt Quick 画廊帧时间基准")
    ap.add_argument("--data", default=DEFAULT_DIR, help="假数据目录")
    ap.add_argument("--records", type=int, default=0, help="只取前 N 条（0=全部）")
    ap.add_argument("--scroll-seconds", type=float, default=10.0)
    ap.add_argument("--jump-seconds", type=float, default=3.0, help="随机跳转阶段时长（压迫委托创建与图片解码）")
    ap.add_argument("--sync", action="store_true", help="关闭异步解码（对照组）")
    ap.add_argument("--visible", action="store_true", help="真实窗口（需要桌面会话）")
    ap.add_argument("--json-out", default="", help="把结果写成 JSON")
    args = ap.parse_args(argv)

    if not args.visible:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QUICK_BACKEND", "software")

    manifest = Path(args.data) / "records.json"
    if not manifest.exists():
        print(f"缺数据：{manifest}\n先跑 python tools/phase0/make_fixtures.py", file=sys.stderr)
        return 2
    records = json.loads(manifest.read_text(encoding="utf-8"))
    if args.records:
        records = records[: args.records]

    binding, QTimer, QGuiApplication, QQmlApplicationEngine, QQuickWindow = load_binding()

    app = QGuiApplication(sys.argv[:1])
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("benchModel", records)
    # 必须在 load() 之前注入：委托创建时就要拿到正确的解码模式
    engine.rootContext().setContextProperty("benchAsync", not args.sync)

    phases = ("idle", "scroll", "jump")
    samples: dict[str, list[float]] = {p: [] for p in phases}
    lags: dict[str, list[float]] = {p: [] for p in phases}
    state = {"phase": "startup", "t0": time.perf_counter(), "first_frame": None}

    t_load = time.perf_counter()
    engine.load(str(HERE / "qml" / "GalleryBench.qml"))
    if not engine.rootObjects():
        print("QML 加载失败：引擎没有根对象", file=sys.stderr)
        return 3

    root = engine.rootObjects()[0]
    # 根对象是 Window：它本身就是 QQuickWindow；万一根是 Item 则取其所属窗口
    window = root if isinstance(root, QQuickWindow) else (
        root.window() if hasattr(root, "window") else None
    )

    if window is None:
        print("拿不到 QQuickWindow，无法采集帧时间", file=sys.stderr)
        return 4

    def on_frame_swapped():
        now = time.perf_counter()
        phase = state["phase"]
        if state["first_frame"] is None:
            state["first_frame"] = now - t_load
        if phase in samples:
            samples[phase].append(now)

    window.frameSwapped.connect(on_frame_swapped)

    # UI 线程阻塞探针：1ms 定时器实际间隔就是 GUI 线程的可用性。
    # 旧版的同步缩略图解码会在这里表现为明显的大间隔。
    lag_state = {"last": time.perf_counter()}

    def on_lag_tick():
        now = time.perf_counter()
        gap = (now - lag_state["last"]) * 1000
        lag_state["last"] = now
        phase = state["phase"]
        if phase in lags:
            lags[phase].append(gap)

    lag_timer = QTimer()
    lag_timer.setInterval(1)
    lag_timer.timeout.connect(on_lag_tick)
    lag_timer.start()

    # 三个阶段全靠翻 QML 侧开关，测量循环里不做 Python→QML 高频调用。
    # 注：软件渲染是「按需重绘」——只有内容变化才出帧，静止阶段帧数天然很少，
    #     所以真正有意义的样本来自后两个阶段。
    steps = [
        ("idle", 1.2, 0.0, False),
        ("scroll", args.scroll_seconds, 2000.0, False),
        ("jump", args.jump_seconds, 0.0, True),
    ]

    mode_label = "同步解码" if args.sync else "异步解码"
    platform = "可见窗口" if args.visible else "offscreen(软件渲染)"
    print(f"绑定 {binding} · {mode_label} · {platform} · {len(records)} 条记录")

    for name, seconds, speed, jumping in steps:
        root.setProperty("scrollSpeed", speed)
        root.setProperty("jumping", jumping)
        state["phase"] = name
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            app.processEvents()
            time.sleep(0.001)
        root.setProperty("scrollSpeed", 0.0)
        root.setProperty("jumping", False)

    result = {
        "binding": binding,
        "async": not args.sync,
        "visible": args.visible,
        "records": len(records),
        "first_frame_ms": round((state["first_frame"] or 0) * 1000, 1),
        "rss_mb": round(rss_mb(), 1),
    }
    for name in phases:
        result[name] = summarize(name, samples[name])
        result[name]["ui_lag"] = summarize_raw(f"{name}_ui_lag", lags[name])

    # property var 取回来是 QJSValue，需要转成 Python 值
    raw_stalls = root.property("jumpStalls")
    if hasattr(raw_stalls, "toVariant"):
        raw_stalls = raw_stalls.toVariant()
    stalls = [float(x) for x in (raw_stalls or [])]
    result["jump_stalls"] = summarize_raw("jump_stalls", stalls)

    print()
    header = f"{'阶段':<8}{'帧数':>7}{'FPS':>8}{'中位ms':>9}{'p95ms':>8}{'p99ms':>8}{'最大ms':>9}{'>16.7':>7}{'>33.3':>7}"
    print(header)
    for name in phases:
        s = result[name]
        if s.get("frames", 0) < 2:
            print(f"{name:<8}{'样本不足（该平台不产生帧交换）':>30}")
        else:
            print(
                f"{name:<8}{s['frames']:>7}{s['fps']:>8}{s['median_ms']:>9}{s['p95_ms']:>8}"
                f"{s['p99_ms']:>8}{s['max_ms']:>9}{s['over_16ms']:>7}{s['over_33ms']:>7}"
            )

    print()
    print(f"{'UI 线程阻塞探针(1ms 定时器实际间隔)':<32}{'样本':>8}{'中位ms':>9}{'p99ms':>8}{'最大ms':>9}{'>50ms':>8}")
    for name in phases:
        lag = result[name]["ui_lag"]
        if lag.get("frames", 0) < 2:
            continue
        print(
            f"{name:<32}{lag['frames']:>8}{lag['median_ms']:>9}{lag['p99_ms']:>8}{lag['max_ms']:>9}{lag['over_50ms']:>8}"
        )

    js = result["jump_stalls"]
    if js.get("frames", 0):
        print()
        print(f"{'随机跳转到出帧耗时':<32}{'次数':>8}{'中位ms':>9}{'p95ms':>8}{'最大ms':>9}")
        print(f"{'jump_stall':<32}{js['frames']:>8}{js['median_ms']:>9}{js['p95_ms']:>8}{js['max_ms']:>9}")
    print(f"\n首帧 {result['first_frame_ms']}ms · 峰值内存 {result['rss_mb']}MB")

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已写入 {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
