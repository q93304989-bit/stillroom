"""Stillroom 应用包（原名 Agnes Studio，2026-09-26 更名；原因见 CHANGELOG.md）。

分层（依赖只能向下，不能向上或成环）：

    app.config   ← 路径 / 密钥 / 偏好，三个唯一入口
    app.net      ← 出网与错误分类
    app.clients  ← 图片 / 视频 / 图床 / LLM 客户端
    app.capabilities ← 能力注册表（工具描述 + 元数据）
    app.services ← 编排（Phase 2）
    app.ui       ← 界面（Phase 3）

本层不含任何 tkinter / Qt 依赖，可在无 GUI 环境完整测试。
"""

__version__ = "1.0.0"
