"""任务路由：把一句意图映射到候选工作流。

## P1 只交付**桩**，而且这是契约里的合法出口

`contracts/mcp-tools.md` §5 规定未命中就返回 `{"match": "none", "candidates": []}`，
Agent 收到后走"换个说法 / 新建 Workflow"。P1 的桩**恒定**返回它 ——
所以这不是占位符，是契约已经允许的那个出口，只是命中率暂时是 0。

真实链路 `retrieval → metadata filter → LLM rerank → threshold` 属 **P2**。
这么切的理由（决策 #7）：注入点的形状应当由**消费者**反向定义。
先写一段注定被 embedding 替换掉的关键词检索，是在猜接口 ——
猜出来的签名一定会被 P2 的真实实现改掉，连带改 MCP 工具与测试。

## 注入点的形状：`MatchFn`

```python
match(intent: str, top_k: int) -> {"match": "candidates"|"none", "candidates": [...]}
```

就这一条函数签名。P2 的真实路由器（含 embedding + rerank）实现它，
`ServerContext.router` 换掉即可，**`tools.py` 一行不用改**。
`top_k` 是位置参数而不是放进 options：契约 §5 的入参就这两个，
多包一层 `options` 只会让 P2 的实现者去猜哪层该放什么。
"""

from __future__ import annotations

from typing import Any, Callable

MatchFn = Callable[[str, int], dict[str, Any]]


def stub_match(intent: str, top_k: int) -> dict[str, Any]:
    """P1 的固定桩。**参数刻意不用** —— 但签名保留，P2 换实现时不用改调用方。

    不 `raise NotImplementedError`：契约要求这个工具在 P1 可用且返回合法出口。
    一个"存在但抛异常"的工具会让 `get_capabilities` 在撒谎。
    """
    return {"match": "none", "candidates": []}


__all__ = ["MatchFn", "stub_match"]
