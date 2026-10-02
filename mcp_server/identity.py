"""身份的**唯一来源**：进程启动配置 → `creator_context`。

契约 §一 只写了这件事的一半：「`params` 里任何身份 / 权限字段都不是合法入参」。
另一半是「那身份到底从哪来」—— **这里**。P1 里它来自进程的启动配置
（`--identity` 参数或 `STILLROOM_IDENTITY` 环境变量），
**没有任何 MCP 方法能改它**，也没有任何 `params` 字段能影响它。

## 为什么是一张表，不是一层 `if`

三种身份的差别只体现在三个**策略字段**上：能力集 / 信任上限 / 能否自激活。
写成 `if identity == "system": …` 的话，这三处的取值会散在分支里，
`get_capabilities` 报给客户端的那一份、`validate_workflow` 判定用的那一份，
迟早不是同一套 —— 而且这种不一致**不会报错**，只会让权限判断悄悄偏掉。

一张 `IDENTITY_PRESETS` 表让它们只有一个出处：改一处，两边一起动。
顺带还有一个好处 —— 预设是**可枚举**的，所以运维能直接看到"这台机器现在是谁"。

## `is_system` 是**派生**的，不是配置项

`validator/workflow_validator.py` 自己算的是
`is_system = bool(creator.get("is_system", False)) or identity == "system"` ——
它给早期调用方留了一个兼容口。**我们刻意不利用那个口子**：
预设表里根本没有 `is_system` 这个字段，它一律由 `identity == "system"` 推出。

理由是安全：`is_system` 一旦是个"可以单独写"的字段，
`{"identity": "agent", "is_system": true}` 就是一次提权 —— 而那是纯手写错误，
不该有可能发生。让它**不可表达**比让它"被校验拒绝"更省事，也更难绕过。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from validator import KNOWN_CAPABILITIES

#: 信任等级由高到低。放在这里是为了让预设表的取值**可读**，
#: 权威定义仍在 `validator/workflow_validator.py::_TRUST_RANK`。
TRUST_TIERS = ("T0", "T1", "T2", "T3")


@dataclass(frozen=True)
class IdentityPreset:
    """一种身份的**策略**。`identity` 与 `is_system` 不在字段里 —— 见模块说明。"""

    identity: str
    allowed_capabilities: tuple[str, ...]
    max_trust_level: str
    can_auto_activate: bool
    summary: str

    def to_creator_context(self) -> dict[str, Any]:
        """转成 `validate_workflow` / `validate_skill` 认的那份 dict。

        每次返回**全新**的 dict 与 list：`ServerContext` 会把它冻上，
        但冻结之前不能有任何调用方共享同一个 list —— 那是"改一处、
        别处跟着变"的经典来路。
        """
        return {
            "identity": self.identity,
            "is_system": self.identity == SYSTEM,   # 派生，不是配置
            "allowed_capabilities": list(self.allowed_capabilities),
            "max_trust_level": self.max_trust_level,
            "can_auto_activate": self.can_auto_activate,
        }


SYSTEM = "system"
HUMAN = "human"
AGENT = "agent"

#: 三种身份的预设策略。取值不是拍脑袋定的，每一条都能追到一个约束：
#:
#: - `system`：唯一能自我授权的身份。`max_trust_level = T3` +
#:   `can_auto_activate = True`，因为 L3 里"自动激活"要求 T3 且 creator 达到 T3
#:   （`workflow_validator` 的 (d) 规则）—— 不给它，就没有任何身份能建自动激活的工作流；
#:   能力集取全集。
#: - `human`：界面上的操作者。能力集同样是全集（人建得了任何工作流），
#:   但信任上限压在 **T2**、且不能自激活 —— 也就是"能建，不能免批准"。
#:   把 T3 留给 `system` 是刻意的：P1 里 T3 还是唯一能触发自动激活的档位，
#:   放开它等于把"免人工批准"变成默认可得。
#: - `agent`：经 MCP 接入的上层 Agent。取值**逐字取自契约 §一的那段示例**
#:   （`{"identity": "agent", "allowed_capabilities": ["llm.call"],
#:   "max_trust_level": "T2", "can_auto_activate": false}`）。
#:   能力集只有 `llm.call`：Agent 默认只被允许"调模型"，
#:   要读写文件、碰仓库得由部署方显式放宽 —— 这是最小权限，
#:   而不是"忘了限制于是给了全部"。
IDENTITY_PRESETS: dict[str, IdentityPreset] = {
    SYSTEM: IdentityPreset(
        identity=SYSTEM,
        allowed_capabilities=tuple(sorted(KNOWN_CAPABILITIES)),
        max_trust_level="T3",
        can_auto_activate=True,
        summary="Stillroom 自身（内核 / 内部调用）；唯一可自授权的身份",
    ),
    HUMAN: IdentityPreset(
        identity=HUMAN,
        allowed_capabilities=tuple(sorted(KNOWN_CAPABILITIES)),
        max_trust_level="T2",
        can_auto_activate=False,
        summary="界面操作者：能力全开，信任上限 T2，不能免人工批准",
    ),
    AGENT: IdentityPreset(
        identity=AGENT,
        allowed_capabilities=("llm.call",),
        max_trust_level="T2",
        can_auto_activate=False,
        summary="经 MCP 接入的上层 Agent：默认只允许调模型（契约 §一 的示例）",
    ),
}

IDENTITY_CHOICES: tuple[str, ...] = tuple(sorted(IDENTITY_PRESETS))


def build_creator_context(identity: str) -> dict[str, Any]:
    """把身份名翻成 `creator_context`。

    **未知身份直接抛 `ValueError`，不回落默认值。** 两条理由：

    1. 这是"身份从哪来"这条链的最后一环。给它一个默认值，
       等于让"忘了配"静默变成"某个身份"——而安全模型里最不该有的
       就是静默落点。启动时一行报错，比运行时所有工具都拒掉便宜得多。
    2. 回落成 `agent`（最小权限）虽然"安全"，但**看起来是好的**：
       服务照常起来、照常能用一部分功能，而部署方的意图（比如 system）
       根本没生效。这种失败比拒绝启动难查一个数量级。
    """
    preset = IDENTITY_PRESETS.get(identity)
    if preset is None:
        raise ValueError(
            f"unknown identity {identity!r}; choose one of {list(IDENTITY_CHOICES)}"
        )
    return preset.to_creator_context()


__all__ = [
    "AGENT",
    "HUMAN",
    "IDENTITY_CHOICES",
    "IDENTITY_PRESETS",
    "SYSTEM",
    "TRUST_TIERS",
    "IdentityPreset",
    "build_creator_context",
]
