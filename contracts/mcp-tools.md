# MCP Tools — v1.0（冻结）

> Agent 与 Stillroom 之间的**唯一**接口。共 11 个工具。
> Stillroom 是「工作流注册中心 + 路由执行引擎」，**不是**对等 A2A 的一方 ——
> 这是 Agent-to-Service：Agent 只能操作 **Workflow Definition** 与 **Execution**，
> 永远不能直接碰 Kernel。

## 一、通用约定

**返回信封**（所有工具一致）：

```json
{ "ok": true,  "data": { } }
{ "ok": false, "error": { "code": "SEMANTIC_INVALID", "message": "…", "path": "pipeline/refine" } }
```

`code` 一律取自 `validator/errors.py` 的 `ErrorCode`，不另造字符串。

**信封是双向的**：成功与失败**都由工具层的统一包装产生**，不由各工具自己写。

| 情形 | `result` 的内容 |
|---|---|
| 成功 | `{"ok": true, "data": { …工具的出参… }}` |
| 业务失败（`StillroomRuntimeError`） | `{"ok": false, "error": {"code": "…", "message": "…", "path"? : "…", "details"?: {…}}}` |
| 编程错误（`TypeError` / `KeyError` …） | **不出现** —— 让它抛，由传输层兜成 `-32603`（见 §五.1） |

推论：**工具函数只返回 `data` 本身**（如 `{"tools": [...]}`），
不自己拼 `{"ok": true, …}`，也不 `try/except` 自己的业务异常 —— 那两件事各只在一处做。
详见 §五.4。

**谁在调用**：每个工具都带 `creator_context`（由 Server 从连接身份推导，**不接受客户端自报**）：

```json
{
  "identity": "agent",              // system | human | agent
  "allowed_capabilities": ["llm.call"],
  "max_trust_level": "T2",
  "can_auto_activate": false
}
```

**`creator_context` 的入口只有一个：Server 进程的启动配置。**

| 阶段 | 规则 |
|---|---|
| 来源 | 进程启动配置：`__main__.py` 读 `--identity` / `STILLROOM_IDENTITY`（**无缺省，必填**），`identity.py::build_creator_context` 翻成策略；进程内**固定不变**（见 §一.2） |
| 传递 | 打包进 `ServerContext`，由 `build_toolset(ctx)` 注入每个工具；**只读**（`__post_init__` 深度冻结：dict → 只读映射，list → tuple） |
| 与 `params` 的关系 | **`params` 里任何身份 / 权限字段都不是合法入参** —— 不是"忽略"，是直接 `INPUT_SCHEMA_INVALID` 拒掉 |
| 强制点 | `mcp_server/tools.py::IDENTITY_FIELDS` 是**禁名表**：`_reject_unknown` 按白名单挡"params 是工具信封"的 10 个工具，`_reject_identity` 按禁名表挡 `create_skill`（它的 params 就是定义文档）。两条路报**同一个码** |

最后一条是刻意的：静默忽略会让**伪造尝试不留痕迹**，而拒掉能让它显形。
这条不写死，将来一定会有人"顺手"从 `params` 里读，**L3 校验与安全模型同时静默失效**。

### 两个 `*_SCHEMA_INVALID` 的分工（`create_skill` 上会同时出现）

`params` 是**工具信封**时按白名单判，违反报 `INPUT_SCHEMA_INVALID`；
`params` 是**定义文档**时（`create_skill`）按定义自身的字段表判，非身份类的意外字段
报 `SCHEMA_INVALID`，并带上 `a skill has exactly [...]` 的提示。

**身份字段是唯一的例外**：它在进定义校验**之前**就被禁名表拒掉，仍报
`INPUT_SCHEMA_INVALID`。这样同一次伪造尝试在 11 个工具上得到同一个码，
客户端为一个安全边界只需写一套判断；否则 `create_skill` 就成了那个"要特判的例外"，
而特判正是规则开始松掉的地方。

纵深防御的另一半理由：`skill_validator._FIELDS` 将来若长出一个与身份**同名**的字段
（例如 `trust_level`），定义校验会**静默放行**它，安全不变式随一次 schema 演化失效。
禁名表不看 `_FIELDS` 怎么演化，所以这条路径不存在。

### 一.1 禁名表（不得出现在任何 `params` 里的字段名）

```
creator_context  identity  is_system  allowed_capabilities  max_trust_level  can_auto_activate  trust_level
```

字面量、可机器比对：`mcp_server/tools.py::IDENTITY_FIELDS` 必须等于这一行，
由 `tests/test_mcp_tools.py::test_identity_fields_match_the_contract` 钉住。
两端都是字面量，改一边不改另一边就红 —— 与 `test_tool_names_match_the_frozen_contract` 同构。

这条纪律的必要性在于禁名表**会漂**：契约新增一个身份字段而实现不改，该字段就被静默放行；
实现多禁一个名字而契约不改，契约里的合法字段就被误拒。两个方向都不会自己响。

### 一.2 身份到底从哪来（安全模型的最后一环）

上面说清了"`params` 不许自报"，这里说清"那从哪来"。
**唯一来源是进程的启动配置**，P1 是 `--identity` 参数或 `STILLROOM_IDENTITY` 环境变量
（见 §五.7）。没有 MCP 方法能改它，没有 `params` 字段能影响它。

三条身份的预设策略（`mcp_server/identity.py::IDENTITY_PRESETS`）：

| identity | `allowed_capabilities` | `max_trust_level` | `can_auto_activate` |
|---|---|---|---|
| `system` | 全部 `KNOWN_CAPABILITIES` | `T3` | `true` |
| `human` | 全部 `KNOWN_CAPABILITIES` | `T2` | `false` |
| `agent` | `["llm.call"]` | `T2` | `false` |

取值不是拍脑袋的，每条都追得到一个既有约束：

- `system` 是**唯一**能自我授权的身份。L3 里"自动激活"要求 `trust_level = T3`
  且 creator 达到 T3 —— 不给它 `T3 + can_auto_activate`，就没有任何身份能建自动激活的工作流。
- `human` 能力全开（人建得了任何工作流）但信任上限压在 `T2`、不能自激活，即"能建，不能免批准"。
- `agent` 的四个取值**逐字取自本文档 §一上面那段示例**，由测试机器比对两边。
  能力集只有 `llm.call`：Agent 默认只被允许调模型，要读写文件、碰仓库得由部署方显式放宽。

**`is_system` 是派生字段，不是配置项。** `validator` 自己算的是
`is_system = bool(creator.get("is_system", False)) or identity == "system"`（给早期调用方留的兼容口），
但预设表里**没有**这个字段，它一律由 `identity == "system"` 推出。
理由是安全：`is_system` 一旦能单独写，`{"identity": "agent", "is_system": true}` 就是一次提权 ——
那是纯手写错误，不该有可能发生。让它**不可表达**比让它"被校验拒绝"更难绕过。

## 二、工具清单

| # | 工具 | 作用 |
|---|---|---|
| 1 | `get_capabilities` | 握手：本 Server 支持的工具、能力清单、身份与信任上限 |
| 2 | `list_skills` | 列原子能力（`skill_id` / 版本 / 所需 capability） |
| 3 | `create_skill` | 写一个原子能力 |
| 4 | `list_workflows` | 列工作流定义（含 `version`、`trust_level`、`active`） |
| 5 | `match_workflow` | 意图匹配：候选 + 置信度；不够就 `none` |
| 6 | `create_workflow` | 提交受限配置（**不是可执行程序**） |
| 7 | `execute_workflow` | 下发执行，`request_id` 幂等 |
| 8 | `retry_execution` | 重试：新 execution + 同 `request_id` + `parent` |
| 9 | `get_execution_status` | 查状态（含步骤、预算用量、artifact 引用） |
| 10 | `abort_execution` | 请求中止（发 `abort_requested`） |
| 11 | `resume_execution` | 从 `WAITING_INPUT` 恢复（发 `resume_requested`） |

### 1. `get_capabilities`

入参：无。
出参：`{ "tools": [...11 个...], "capabilities": ["llm.call", ...], "identity": "agent", "max_trust_level": "T2", "can_auto_activate": false, "protocol_version": "1.0" }`

### 2. `list_skills`

入参：`{ "filter": { "capability": "llm.call" } }`（可选）。
出参：`{ "skills": [{ "skill_id": "…", "version": 2, "description": "…", "required_capabilities": ["llm.call"] }] }`

### 3. `create_skill`

入参：`{ "skill_id": "…", "version": 1, "description": "…", "required_capabilities": ["llm.call"], "io_contract": { … } }`
出参：`{ "skill_id": "…", "version": 1, "status": "registered" }`
约束：`required_capabilities` 必须 ⊆ creator 的 `allowed_capabilities`，否则 `CAPABILITY_DENIED`。

技能的 **params 就是定义文档本身**（没有 `{ "skill": … }` 这层信封），所以形状校验只有
一处：`validator/skill_validator.py`。非身份类的意外字段报 `SCHEMA_INVALID`；
身份字段由禁名表提前拒掉，报 `INPUT_SCHEMA_INVALID`（见 §一的分工说明）。

### 4. `list_workflows`

入参：`{ "include_inactive": false }`。
出参：`{ "workflows": [{ "workflow_id": "article_generation", "version": 3, "display_name": "…", "trust_level": "T2", "active": true }] }`

### 5. `match_workflow`

入参：`{ "intent": "写一篇…", "top_k": 3 }`
出参（命中）：`{ "match": "candidates", "candidates": [{ "workflow_id": "…", "version": 3, "confidence": 0.82, "reason": "…" }] }`
出参（未命中）：`{ "match": "none", "candidates": [] }`

链路：`retrieval → metadata filter → LLM rerank → threshold`。
**低于阈值就返回 `none`，由 Agent 自己决定「换个说法」还是「新建 Workflow」**——
Server 不替 Agent 猜，也不偷偷降级到默认工作流。超时给 `MATCH_TIMEOUT`。

#### P1 阶段说明（`router.py` 的落点）

`match_workflow` 在 P1 返回固定桩：

```json
{ "match": "none", "candidates": [] }
```

真实检索与 rerank 属 **P2**。**这是协议的合法出口，不是占位符** ——
契约本身就规定未命中返回 `none`，Agent 收到它应走"换个说法 / 新建 Workflow"路径，
与"匹配过但低于阈值"的处理完全一致。

因此 P1 的 `mcp_server` 里 `match_workflow` 是**最小骨架**，
`router.py`（检索 + LLM rerank 注入点）与 `match_workflow` 的实现同批落地。
这样做的理由：注入点的形状应由**消费者**（MCP Server 层）反向定义，
而不是先写一段注定要被 embedding 方案替换掉的关键词检索去猜接口。
`match_workflow` 是 11 个工具之一，P1 交付时必须存在 —— 存在但恒 `none`。

### 6. `create_workflow`

入参：`{ "workflow": { …Workflow Definition… }, "activate": false }`
出参：`{ "workflow_id": "…", "version": 1, "status": "pending_activation" | "active", "trust_level": "T2" }`

写入的必须是有六步全显式 + `step_overrides` 全显式的受限配置（见 `schemas/workflow.schema.json`）。
落库前依次过四层：

```
L1 Schema → L2 Semantic → L3 Trust/Policy → L4 Metadata → 沙箱 → 人工激活
```

对应错误码：`SCHEMA_INVALID` / `SEMANTIC_INVALID` / `INSUFFICIENT_TRUST` / `CAPABILITY_DENIED` / `METADATA_FORBIDDEN`。

### 7. `execute_workflow`

入参：`{ "workflow_id": "…", "version": 3, "request_id": "req_…", "input": { … }, "metadata": { … } }`
出参：`{ "execution_id": "exec_…", "request_id": "req_…", "status": "PENDING" }`

**幂等不变式**：`request_id` 永久绑定首次 `execution_id`；
同一个 `request_id` 再调一次，永远返回**同一个** execution，不会新起一次执行、也不会报错。
`input` 必须过 `io_contract.input_schema_ref`，否则 `INPUT_SCHEMA_INVALID`。
工作流不存在 / 未激活：`WORKFLOW_NOT_FOUND` / `WORKFLOW_NOT_ACTIVE`。

### 8. `retry_execution`

入参：`{ "execution_id": "exec_…", "request_id": "req_…" }`（**没有 override 字段**）
出参：`{ "execution_id": "exec_…", "parent_execution_id": "exec_…", "status": "PENDING" }`

新 execution 复用原 `input_snapshot`（逐字节），并继承 `request_id` 与 parent。
错误码见 `contracts/execution-state-machine.md` 第四节。

### 9. `get_execution_status`

入参：`{ "execution_id": "exec_…" }`
出参：`{ "status": "RUNNING", "current_step": "generate", "steps": [{ "step": "understand", "status": "done" }], "budget": { "tokens_used": 3120, "max_tokens": 20000 }, "artifacts": [{ "artifact_id": "art_…", "kind": "image", "uri": "…" }], "error": null }`
不存在：`EXECUTION_NOT_FOUND`。

**这是 11 个工具里唯一一处「局部降级」，且它有明确的边界 —— 别当成多余的 `try/except` 删掉。**

出参里只有一个字段来自**注册域**：`budget.max_tokens`（取自工作流定义的 `resource_policy`）。
其余全部来自协议域（这次执行自己）。所以当定义读不出来时（`definition_ref` 失效、artifact 被移走、
registry 行被下架），正确处理**不是**把整条请求判失败 —— 调用方问的是"这次执行现在怎么样"，
答案是**有的**，只是其中一个附加项拿不到。

| 问题 | 答案 |
|---|---|
| 降级产物是什么 | `budget.max_tokens = null`。**不是 `0`** —— `0` 会被读成"预算为零/已耗尽"，是假话；`null` 的含义是"这个值读不到" |
| 其余字段 | 照常返回。`status` / `current_step` / `steps` / `error` / `artifacts` 一个不少 |
| 整条请求 | **仍然 `{"ok": true}`**。降级不等于失败 |
| 为什么不走 `_bind` | `_bind` 归一化的是**整个调用失败**（`{"ok": false}`）。这里调用是成功的，把它变成失败才是错 —— 那是把"定义可用性"和"执行状态"两件事混在一起 |
| 可观测性 | 降级必须**留痕**：`emit` 一条日志到 stderr。降级而无声，是"静默降级"，比报错更难查 |
| 强制点 | `tools.py::_max_tokens_of` —— `tools.py` 里仅有的两条 `except` 之一（另一条是 `_bind`），由 `test_the_binder_does_not_catch_broad_exceptions` 同时断言"总数恰好 2"与"类型只接 `StillroomRuntimeError`" |

`tokens_used` 与 `artifacts` 在 P1 **如实为空**（`0` / `[]`）：stub kernel 真的不消耗 token、
也不产出真产物。按"不为工作流定义伪造 Artifact Manifest"的先例，这里也不伪造
`artifact_id` / `kind` / `uri`。它们属 P3 的真实内核。

### 10. `abort_execution`

入参：`{ "execution_id": "exec_…", "reason": "…" }`
出参：`{ "status": "ABORT_PENDING" }`（若当前无步骤在跑，直接 `ABORTED`）
约束：`step_overrides.<step>.cancellable=false` 的步骤**不可中止**，只会停在 `ABORT_PENDING` 等它收尾。
终态上调用：`ALREADY_TERMINAL`。

### 11. `resume_execution`

入参：`{ "execution_id": "exec_…", "input": { … } }`（补上缺的那份输入）
出参：`{ "status": "RUNNING" }`
非 `WAITING_INPUT` 调用：`NOT_WAITING_INPUT`。

## 三、Agent 不能做的事（逐条对应到错误码 / 设计）

| 禁令 | 落点 |
|---|---|
| 不能改六步顺序 | schema `pipeline` 固定 6 键 + `additionalProperties:false` |
| 不能自定义 DAG / 循环 / 递归 / 子工作流 / if-else | schema 里根本没有这些字段，写进去即 `SCHEMA_INVALID` |
| 不能改执行内核 | Kernel 顺序由代码掌控，MCP 侧无接口 |
| 不能动态提升 trust level | L3：`declared_trust ≤ creator.max_trust_level`，且 T0/T1 只有 system 能声明 |
| 不能 `override_input` 重试 | `retry_execution` 入参里没有该字段；复用原 `input_snapshot` |
| metadata 只能描述，不能改变执行 | L4 递归扫描，`workflow_override` / `permissions` / `trust_level` / `pipeline` / `budget_override` / `execution_command` / `capability_grant` 一律 `METADATA_FORBIDDEN` |
| 不能直接操作 Execution 状态 | 状态只能由 Engine、`abort_execution`、`resume_execution` 触发 |

## 四、核心调用链

```
Agent / Human
    ↓ MCP（11 个工具）
Stillroom MCP Server
    ↓
Task Router：retrieval → metadata filter → LLM rerank → threshold
    ↓
Workflow Registry：workflow + version + trust level
    ↓
Execution State Machine：幂等 / 超时 / 取消 / 恢复 / 预算
    ↓
Stillroom Kernel：六步 Pipeline（理解 → 找参考 → 生成 → 评估 → 精修 → 交付）
    ↓
Artifact / Replay
```

Agent 下发任务后由 Stillroom 做意图识别：
有合适工作流 → 返回候选 + 置信度；没有 → 返回 `none`，新建与否由 Agent 决定。

## 五、传输层约定（JSON-RPC 2.0 over stdio）

工具层的信封（§一）之上还有一层：Agent 与 Server 之间的**传输协议**。
实现见 `mcp_server/jsonrpc.py`，不变式由 `tests/test_mcp_jsonrpc.py` 锁死。
这一层只做分帧与信封校验，**不认识任何工具**。

用的是 **newline-delimited JSON-RPC 2.0**（一行一条消息，**没有** LSP 那种
`Content-Length` 头）。

### 五.1 错误码分工（最容易混的一处）

| 层 | 错误 | 响应形态 |
|---|---|---|
| **传输层** | 解析失败 / 信封无效 / 方法不存在 / `params` 不是对象 | `{"jsonrpc":"2.0","id":…,"error":{"code":-32xxx,…}}` |
| **业务层** | `SCHEMA_INVALID` / `EXECUTION_NOT_FOUND` / `RETRY_EXHAUSTED` … | `{"jsonrpc":"2.0","id":…,"result":{"ok":false,"error":{…}}}` |

`error` 字段表示**调用链断了**（方法不存在、信封坏掉）；
`{"ok":false}` 表示**调用链通了、工具告诉你输入不合法**。
混在一起，客户端就没法区分"这个方法不存在"与"我的参数写错了"。

| 码 | 含义 |
|---|---|
| `-32700` | 解析失败（含超长行、纯空白行、`NaN`/`Infinity`） |
| `-32600` | 信封无效（缺/错 `jsonrpc`、`method` 非字符串、`id` 类型非法、**批量请求**） |
| `-32601` | 方法不存在（由 `dispatch.py` 判定） |
| `-32602` | `params` 存在但不是对象 |
| `-32603` | `dispatch` 抛了未捕获异常，或返回了非 dict |

### 五.2 `id` 语义

| 客户端发 | 服务端回 |
|---|---|
| `"id": 1` / `"id": 1.5` | 原值原**类型**回显，不做任何规范化 |
| `"id": "1"` | `"id": "1"` —— 与数字 `1` 是**不同**的 ID，绝不强转 |
| `"id": null` | 合法 ID，**必须响应** |
| 无 `id` 字段 | **通知**：执行但**不响应** |

判定用 `"id" in message`，**不是**真值判断 —— `null` 与"缺字段"是两回事。

**信封坏掉时不能借"缺 id"当通知吞掉**：通知的定义是"*well-formed* 的请求但不带 id"。
信封本身不合法就无从知道它本来是什么，必须回一条（`id: null`，或 `id` 可用时用它）。

### 五.3 十一条边界

| # | 边界 | 处理 |
|---|---|---|
| 1 | 帧格式 | 换行分隔；一行一条；消息内不得有裸 `\n` |
| 2 | 超长行 | 有上限（默认 4 MiB 字符）；**整行丢弃**并回 `-32700`，残余不得当成下一条消息 |
| 3 | 批量请求（顶层数组） | **显式拒绝**：回一条 `-32600`。不逐条处理（会破坏"一条入一条出"）、不静默丢弃（会让客户端卡死） |
| 4 | `NaN` / `Infinity` / `-Infinity` | 入口即拒（`-32700`）。Python 的 `json.loads` 默认收下它们，放过去就会在**离错误源头很远**的仓库层才炸 |
| 5 | 空行 | 只认**真空行**（`"\n"`）。`"   \n"` 是畸形数据，按解析失败处理并记 stderr —— JSON 里空白不是合法值 |
| 6 | `jsonrpc` 字段 | 必检。缺失或非 `"2.0"` → `-32600`，且**不回退**成"当它没写"或"当它是通知" |
| 7 | `params` | 缺失 = `{}`；存在但非对象 → `-32602`；**内部字段不合法属业务层**，不在此拒绝 |
| 8 | `dispatch` 抛异常 | 由传输层兜底为 `-32603`，堆栈只进 stderr、**不回显**（异常文本可能带路径 / SQL / 内部结构） |
| 9 | 单条坏消息 | 记 stderr、回错误、**继续读下一条**。只有 stdin 读不出或 stdout 写不进才非 0 退出 |
| 10 | stdout 纪律 | 协议帧是 stdout 的**唯一**内容。日志 / 诊断 / traceback 一律 stderr。判据是**标准流**（不限 `sys`）—— 默认 `log` 就兜底写 stderr，由 AST 源码扫描锁死 |
| 11 | 并发 | **严格串行**：读到一条处理一条。`ExecutionRepository` 是单连接 + RLock，多线程会踩锁 |

### 五.4 两层的分工（`dispatch.py` / `tools.py`）

| 模块 | 职责 |
|---|---|
| `dispatch.py` | **只有一条判定**：方法名在不在表里。不在 → `-32601`；在 → 调它，返回值**原样**作为 `result` |
| `tools.py` | 11 个工具的业务实现。**业务失败 → 返回 `{"ok":false,"error":{…}}`；编程错误 → 让它抛**，由传输层兜成 `-32603` |

这里的方法表由 `mcp.py` 装配（三个 MCP 方法，见 §五.7）——
`dispatch.py` 对它装的是什么一无所知。

推论（这条不守住，11 个工具就会各自长出一半协议逻辑）：

- `params` 缺字段 / 类型不对 / 值越界 → **不是** `dispatch.py` 的错误，由 `tools.py`
  返回 `INPUT_SCHEMA_INVALID`（走 `result`）。
- 工具函数**永不构造 JSON-RPC 错误对象**。
- `dispatch.py` 的**方法表**是注入的，它不 import `tools` / `mcp` / `runtime` / `validator`。
- 匹配**精确**，不做大小写 / 空白 / 连字符归一化 —— fuzzy 匹配会让拼错的
  `execute_workflow` **静默命中另一个工具**，比报错危险得多。

### 五.5 单一 binder，与守住它的反证

11 个工具的**信封**与**异常归一化**只在 `build_toolset` 里做一次（`_bind`），
不靠 11 个 `@tool` 装饰器各自记得 —— 装饰器漏一个在代码里看不出来，
而 binder 遍历的就是 `TOOLS` 那张唯一的表。

| 抛出 | 出口 |
|---|---|
| `StillroomRuntimeError`（含 `RepositoryError` / `KernelError` / `InputError`） | `{"ok":false,"error":{"code","message","path"?}}`；`details` 只进 stderr |
| 其它（`TypeError` / `KeyError` …） | 一路抛到 `serve()` → `-32603`，堆栈只进 stderr |

边界**只切在 `StillroomRuntimeError` 这一层**，两个方向都错：

- 窄到某个子类（如只接 `RepositoryError`）→ `KernelError` 这类兄弟异常伪装成 `-32603`，
  客户端把"你这个状态下不能这么做"读成"服务端内部炸了"；
- 宽到 `Exception` → 工具内部的真缺陷伪装成业务失败，**从日志里消失**。

`tools.py` 里 `except` 子句**恰好 2 条**（`_bind` 的归一化 + `get_execution_status`
读定义时的局部 catch），且都只接 `StillroomRuntimeError` —— 由 AST 源码扫描钉住
（行为测试只能证明"当前没写歪"，证不了"下次不会写歪"）。

**反证（变异测试）在 `tools/prove_mcp_server.py`**：对 `tools.py`（工具层 11 个场景）
与 `identity.py` / `__main__.py`（入口层 5 个场景）施加定点变异 ——
异常边界放宽 / 收窄、信封旁路、清单漂移、`get_capabilities` 手抄清单、身份字段注入、
bool 档失效、幂等状态写错、`input_mismatch` 静默、resume 前置检查拿掉、
`is_system` 变成可写字段、未知身份回落、预设偷偷放宽能力集、清理异常顶掉退出码、
stdin 不容错解码 —— 断言对应用例**必须变红**：

```
.venv/Scripts/python.exe tools/prove_mcp_server.py            # 全部场景
.venv/Scripts/python.exe tools/prove_mcp_server.py 信封 清单   # 按名字筛
```

脚本先跑一遍基线（未变异必须全绿），再逐个变异并**逐条**报告哪条用例抓住了它。
两个场景带 `stay_green`：那两条断言**故意不会变红**，因为"某条断言抓不住这个变异"
本身就是一条结论，钉在脚本里比删掉它诚实。

### 五.6 进程入口：配置来源与退出码

`python -m mcp_server` 是唯一的进程入口。三段分工（`__main__.py` → `stdio.py`）：

| 层 | 只做 |
|---|---|
| `__main__.py` | 读 argv / 环境变量 / 缺省路径、配标准流编码、把退出码交给 `sys.exit` |
| `stdio.py` | `build_server_context`（身份 + 路径 → `ServerContext`）、`run`（接到 `serve`）、`close_quietly` |
| `jsonrpc.py` | 分帧与主循环。**收注入的 `reader` / `writer`**，不认识标准流 |

**参数 > 环境变量 > 缺省**：

| 项 | 参数 | 环境变量 | 缺省 |
|---|---|---|---|
| 库 | `--db` | `STILLROOM_PROTOCOL_DB` | `%APPDATA%\Stillroom\protocol.db`（无 `APPDATA` 时 `~/.stillroom/protocol.db`） |
| 产物目录 | `--artifacts` | `STILLROOM_ARTIFACTS_DIR` | **不给** —— 由 `WorkflowRegistry` 用 `<库所在目录>/artifacts` |
| 身份 | `--identity` | `STILLROOM_IDENTITY` | **无缺省，必填** |

产物目录刻意没有缺省值：默认值只该有一个出处，而那条规则已经在 `registry.py` 里
（"一个库 + 一个目录 = 完整备份单元"）。在这里再写一遍就是第二个出处。

**父目录与产物目录都自动创建**（`schema.connect` 与 `ArtifactStore.__init__` 各自的
`mkdir(parents=True, exist_ok=True)`）—— 入口层不需要也不应该再做一次。
**库的 schema 比代码新**（`{scope} schema vN is newer than code vM`）在构造
`ServerContext` 时就抛 `RepositoryError(SCHEMA_INVALID)` → 启动失败，不做任何迁移尝试。

退出码：

| 码 | 含义 |
|---|---|
| `0` | 正常：stdin 读到 EOF |
| `1` | 运行期不可恢复 IO（stdout 写不出），或启动失败（schema 太新、路径不可用） |
| `2` | 命令行 / 配置错误（缺 `--identity`、未知身份、`--db` 指向目录） |
| `130` | Ctrl-C |

`1` 与 `2` 分开：`2` 是"你命令行写错了"，`1` 是"这台机器 / 这个库有问题"。

**清理不参与退出码。** `close_quietly` 在 `finally` 里跑，任何失败只记 stderr：
`finally` 里抛出的异常会顶掉 `serve()` 的返回值，于是"stdout 写不出去"会被报成
"关库失败"，根因丢失。这与 `rollback_quietly` 是同一条规矩 —— **清理阶段的失败
不许盖掉真正的结果**。

**标准流的编码在入口层钉死**（`__main__.py::configure_streams`），两件事在别的层做不了：

1. stdin 用 `errors="replace"`。否则一个坏字节让 `readline()` 抛 `UnicodeDecodeError`，
   它穿过整个 `serve()`，把"一条坏消息"升级成"服务挂了"（违反 §五.3 边界 #9）。
   换成 U+FFFD 之后它只是一行解析失败 —— `-32700`，循环继续。
2. stdin / stdout 用 `newline="\n"`。Windows 文本模式会把 `\n` 翻成 `\r\n`，
   于是每帧尾巴多一个 `\r`。宽容的客户端看不出来，严格按行切分的客户端会。
   分帧规则是本协议自己定的，那就得由本协议保证字节流真的长那样。

`--help` 是唯一允许写 stdout 的东西 —— 它在进入服务模式**之前**就退出了。

### 五.7 线协议：**三个方法**（工具名不是方法名）

stdio 上只有三个方法 —— 这是 MCP 的规定，也是"能被 MCP 客户端连上"的硬条件：

| 方法 | `params` | `result` |
|---|---|---|
| `initialize` | 客户端自定（`protocolVersion` / `capabilities` / `clientInfo`） | `{protocolVersion, capabilities, serverInfo}` |
| `tools/list` | `{}` 或 `{"cursor": …}` | `{"tools":[{"name","description","inputSchema"}]}` |
| `tools/call` | `{"name": <工具名>, "arguments": <对象，可缺省>}`（`_meta` 忽略） | `{"content":[{"type":"text","text":…}],"isError":bool}` |

**11 个工具名不是方法名。** 客户端发 `{"method":"get_capabilities"}` 会拿到 `-32601`：
那个名字只作为 `tools/call` 的 `params.name` 存在。装配顺序（`stdio.run`）：

```
build_toolset(ctx)    → 11 个绑好 ctx 的工具
make_method_table(…)  → 3 个方法（+ 4 个通知）+ tools/list 的清单
make_dispatch(…)      → 方法表 → 路由（不在表里 → -32601）
serve(reader, writer, dispatch)
```

`dispatch.py` 是**通用**路由，只认"名字在不在表里"，对表里装什么一无所知；
`mcp.py` 是唯一的适配层。两者各自被单测覆盖。

**错误码分工（补充 §五.1）**：

| 情形 | 走哪 |
|---|---|
| 方法名不认识 | `error` `-32601`（`dispatch.py`） |
| `tools/call` 的 `name` / `arguments` 形状不对、出现多余字段 | `error` `-32602` + `data.reason`（`mcp.py`） |
| 工具返回 `{"ok": false}`（**含入参不合法**） | `result` 里 `isError: true`（`tools.py`） |

`name` 不认识报 `-32602` 而不是 `-32601`：`tools/call` 这个方法**在**，错的是参数。
这正是 MCP 的规定，也把"服务端没这个方法"与"我工具名写错了"分开了。

**`-32602` 必须带 `data.reason`。** 一个码扛了四种客户端反应完全不同的情形，
只给 `message` 的话客户端只能靠字符串匹配分支（改一个字就崩）。取值：

| `data.reason` | 何时 | 附带的 `data` | 客户端该做什么 |
|---|---|---|---|
| `unknown_tool` | `name` 是字符串但表里没有 | `name`、`available`（全部 11 个） | 检查**工具名**拼写 |
| `invalid_name` | `name` 缺失 / 非字符串 / 空串 | —— | 检查调用形状 |
| `invalid_arguments` | `arguments` 存在但不是对象 | `got` | 检查调用形状 |
| `unexpected_param` | 出现不认识的字段（很可能拼错） | `unexpected` | 检查字段名拼写 |

`tools/list` 的「多余字段」与 `tools/call` 共用 `unexpected_param`。
取值全集以 `mcp.INVALID_PARAMS_REASONS` 为准，与上表**机器比对**
（`tests/test_mcp_methods.py`），少一个多一个都红。

> ★ **`arguments` 里的字段缺失 / 类型不对，不走 `-32602`。**
> 那是工具自身的入参问题，由 `tools.py` 返回
> `{"ok": false, "error": {"code": "INPUT_SCHEMA_INVALID", …}}`，
> 包在 `result` 的 `isError: true` 里（见 §一 的错误码表）。
> 两者都是"参数不行"，但一层是**协议调用形状**、一层是**业务语义**：
> 混起来会把"服务器拒绝执行"说成"你把 JSON-RPC 调用写错了"。
> 客户端要处理的"参数校验失败"走 `isError` 那条路，不是 `-32602`。

`isError` 只认 `{"ok": true}` 一种成功（判据 `is True`，不是真值判断）——
`{"ok": 1}` / `{"ok": "true"}` / `{"ok": null}` / 缺 `ok` 一律算失败。
写错信封的产物不会被当成成功放过去。

**通知**：`notifications/initialized` / `cancelled` / `progress` / `roots/list_changed`
登记为通知（返回 `{}`）。`serve()` 对不带 `id` 的消息本来就不响应，登记它们
只为让 stderr 不说出"unknown method"这句错话。

**协议版本**：本服务声明 `2024-11-05`（唯一受支持版本）。客户端请求的版本受支持就原样回；
不受支持就回本服务的版本并记 stderr，由客户端决定是否继续（MCP 的版本对齐规则）。

**P1 的两处如实留白**（不是漏做）：

1. `inputSchema` 一律 `{"type":"object","additionalProperties":true}`。
   不为 11 个工具手抄 11 份 JSON Schema —— 那是会和实现漂移的第二真理，
   而真正的入参校验在服务端（`tools.py`，错误码取自 `ErrorCode`）。
   人读的入参说明就是 §二。
2. 不做"未 `initialize` 就不许调工具"的门禁：`serve()` / `dispatch.py` 都是无状态的，
   加握手状态等于引入第二个会话模型。

`tools/list` 的 `description` 取自 `mcp.TOOL_SUMMARIES`，其键集与 `tools.TOOLS`
**完全一致**（`tests/test_mcp_methods.py` 锁死）—— 与 §一.1 的禁名表同一套路：
**单一来源 + 两端同步测试**。而 `tools/list` 的**清单**直接取自工具表本身，
不另存常量，所以"清单说有、`tools/call` 却不认"这种漂移无处可藏。

## 六、与 P1 完成标准的关系

P1 的验收线**不是**「某个桌面客户端能调通」，而是**整条链路在无 UI 环境下端到端可测**（headless，不依赖 PySide6）。
所以 Server 只依赖 `validator/` 与 `schemas/`，不含任何 Qt 引用。
