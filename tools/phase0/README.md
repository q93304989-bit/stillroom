# Phase 0：流畅度验证脚手架

目的：在写正式代码之前，用实测数字回答一个问题——

> Qt Quick 能不能在 500 条记录的画廊里，做到滚动 ≥55fps、UI 线程单帧任务 ≤4ms？

## 三个脚本

| 脚本 | 作用 |
|---|---|
| `make_fixtures.py` | 生成 500 条假记录（真实 JPEG 文件 + 记录清单），用于灌满画廊 |
| `bench_qtquick.py` | Qt Quick 原型：500 项 `GridView` + 异步缩略图，采集帧时间、内存、首帧耗时 |
| `bench_legacy_thumb.py` | 旧版机制对照：PIL 同步解码 + LANCZOS 缩放的单次耗时（旧版 `_thumb_for` 的做法） |

## 怎么跑

```powershell
# 1. 造数据（默认落在 %TEMP%\agnes_phase0，不污染仓库）
python tools/phase0/make_fixtures.py --count 500 --size 1024x768

# 2. 旧版缩略图机制的单次成本
python tools/phase0/bench_legacy_thumb.py

# 3. Qt Quick 原型（默认 offscreen，无需窗口权限）
python tools/phase0/bench_qtquick.py

#     同步解码对照：把 asynchronous 关掉，看同一套界面会掉到多少
python tools/phase0/bench_qtquick.py --sync

#     真实窗口 + 真实 vsync（需要在桌面会话里跑）
python tools/phase0/bench_qtquick.py --visible
```

## 说明

- 本机当前用的是 PyQt5 5.15 + Qt 5.15（已装），脚本优先用 PySide6、缺失时回退 PyQt5；
  正式项目用 PySide6 (Qt 6)，Phase 0 结论按「架构是否成立」判断，绝对值以窗口模式实测为准。
- `offscreen` 模式测的是 UI 线程的 CPU 侧成本（委托创建、模型更新、图片解码调度），
  没有 vsync，绝对帧率不代表真机表现；`--visible` 才是真数字。
- 假数据全部写在临时目录，可随时删除。
