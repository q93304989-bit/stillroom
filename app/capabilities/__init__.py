"""能力注册表：把手动流程、未来的确定性工作流都需要的「能力声明」集中一处。

为什么现在就要它（即使本次不做工作流）：

- 否则「生成一张图」这件事会在界面、工作流、脚本里各写一遍，早晚出现三种行为；
- 平台硬限制（视频每分钟 1 个任务、参考图最多 5 张、仅公网 URL）必须写在能力元数据里，
  调度器与界面提示都从这里取，不再各处硬编码；
- 字段刻意对齐 MCP 的 tool 语义，将来想把能力暴露给外部工具/agent，直接导出即可。
"""

from app.capabilities.registry import (
    SIDE_NETWORK,
    SIDE_PAID,
    SIDE_UPLOAD,
    SIDE_DISK_READ,
    ToolRegistry,
    ToolSpec,
    build_registry,
)

__all__ = [
    "ToolRegistry",
    "ToolSpec",
    "build_registry",
    "SIDE_NETWORK",
    "SIDE_UPLOAD",
    "SIDE_PAID",
    "SIDE_DISK_READ",
]
