# Execution State Machine — v1.0（冻结）

> 契约文件。实现见 `validator/state_machine.py`，不变式由 `tests/test_state_machine.py` 锁死。
> **纯函数**：不碰数据库、时钟、线程；落库由 `runtime/repository.py` 负责
> （`verify_consistency()` 会把本节"缓存列的折叠规则"逐列验回去）。

## 一、状态集合（9 个）

| 状态 | 含义 | 终态 |
|---|---|---|
| `PENDING` | 已登记，尚未开跑 | 否 |
| `RUNNING` | 引擎正在跑当前步骤 | 否 |
| `WAITING_INPUT` | 暂停等人工输入（`input_required` 进入） | 否 |
| `ABORT_PENDING` | 已请求中止，等当前步骤自己收尾 | 否 |
| `COMPLETED` | 六步跑完并交付 | **是** |
| `FAILED` | 引擎报错终止 | **是** |
| `TIMEOUT` | 超过 `max_execution_time` | **是** |
| `ABORTED` | 中止生效 | **是** |
| `BUDGET_EXCEEDED` | 触到 `resource_policy` 上限 | **是** |

## 二、转移表

事件分三类来源，除此之外**没有任何路径**能改状态：

| 事件 | 来源 |
|---|---|
| `engine_started` / `engine_step_ended` / `engine_completed` / `engine_failed` / `engine_timeout` / `budget_exceeded` / `input_required` / `input_provided` | **Engine**（内核自己发） |
| `abort_requested` | `abort_execution` |
| `resume_requested` | `resume_execution` |

| 当前状态 | 事件 | 新状态 |
|---|---|---|
| `PENDING` | `engine_started` | `RUNNING` |
| `PENDING` | `abort_requested` | `ABORTED` |
| `PENDING` | `engine_timeout` | `TIMEOUT` |
| `PENDING` | `budget_exceeded` | `BUDGET_EXCEEDED` |
| `RUNNING` | `engine_step_ended` | `RUNNING` |
| `RUNNING` | `engine_completed` | `COMPLETED` |
| `RUNNING` | `engine_failed` | `FAILED` |
| `RUNNING` | `engine_timeout` | `TIMEOUT` |
| `RUNNING` | `budget_exceeded` | `BUDGET_EXCEEDED` |
| `RUNNING` | `input_required` | `WAITING_INPUT` |
| `RUNNING` | `abort_requested` | `ABORT_PENDING` |
| `WAITING_INPUT` | `input_provided` | `RUNNING` |
| `WAITING_INPUT` | `resume_requested` | `RUNNING` |
| `WAITING_INPUT` | `abort_requested` | `ABORTED` |
| `WAITING_INPUT` | `engine_timeout` | `TIMEOUT` |
| `WAITING_INPUT` | `budget_exceeded` | `BUDGET_EXCEEDED` |
| `ABORT_PENDING` | `engine_step_ended` | `ABORTED` |

表以外的组合：非终态返回 `INVALID_TRANSITION`（**state 不变**），终态返回 `ALREADY_TERMINAL`。

## 三、四条不变式

1. **`ABORT_PENDING` 是正式状态，出口唯一**：只有 `engine_step_ended` → `ABORTED`。
   中止请求不会把正在跑的步骤掐断，而是等它自己收尾 —— 这是「不留半成品」的代价，
   也是它能被 Replay 的原因。
2. **终态固定五个**：`COMPLETED` / `FAILED` / `TIMEOUT` / `ABORTED` / `BUDGET_EXCEEDED`。
   进入终态后任何事件都只得到 `ALREADY_TERMINAL`（不抛异常，便于 MCP 侧原样透出）。
3. **retry 无 override**：见下节，`COMPLETED` 一律拒绝，`BUDGET_EXCEEDED` 默认拒绝。
4. **`WAITING_INPUT` 只能由 `input_required` 进入**，由 `input_provided` / `resume_requested` 离开；
   `resume_requested` 打在别的状态上给 `NOT_WAITING_INPUT`（不是笼统的 `INVALID_TRANSITION`，
   因为调用方需要知道「不是不能恢复，而是当前没在等输入」）。

## 四、retry 策略

`retry_execution` **不带 override 参数**，永远复用原 `input_snapshot`，
新 execution 继承 `parent_execution_id` 与同一个 `request_id`。

判定顺序（先给最可行动的原因，避免调用方看到笼统错误）：

```
非终态 →  COMPLETED →  BUDGET_EXCEEDED 未抬预算 →  未归类终态 →  次数耗尽 →  允许
```

| 判定 | 错误码 |
|---|---|
| 不是终态 | `NOT_TERMINAL` |
| `COMPLETED` | `RETRY_NOT_ALLOWED_FOR_COMPLETED` |
| `BUDGET_EXCEEDED` 且未抬预算 | `RETRY_BUDGET_NOT_RAISED` |
| `retries_used >= max_retries` | `RETRY_EXHAUSTED` |
| 该 attempt 槽位已被占（见下） | `RETRY_RACE_LOST` |

允许 retry 的终态：`FAILED` / `TIMEOUT` / `ABORTED`；
`BUDGET_EXCEEDED` 只有在 `budget_raised=true`（调用方显式抬高预算）后才进入次数判定。

### `RETRY_RACE_LOST` 与 `RETRY_EXHAUSTED` 是两件事

`RETRY_EXHAUSTED` = 次数用完了（计数判定），`RETRY_RACE_LOST` = 想占的 attempt 槽位已被别的行占了。
分开报是因为二者对调用方的含义不同，也为了让统计能区分"预算/次数拒绝"与"竞态拒绝"。

**注意：写事务是 `BEGIN IMMEDIATE`，同一时刻只有一个连接持写锁**，所以两个并发
`retry_execution` 不会真的相撞 —— 后到的那个会先阻塞、等赢家提交后重新计数，
于是它得到的是 `RETRY_EXHAUSTED`（次数确实被用掉了），**不是** `RETRY_RACE_LOST`。
`RETRY_RACE_LOST` 是防御性分支：只有绕开本仓库的事务边界（自建连接直写、
或库里已有同名 attempt 的历史行）才会触发。谁都不该为它写重试逻辑。

### 谁提供 `budget_raised`

`budget_raised` 是 **Stillroom 内部的策略输入**，不是客户端参数：
`contracts/mcp-tools.md` 里 `retry_execution` 的入参只有 `{ execution_id, request_id }`，
Agent 无法传递它，也无法为自己提额。判定「预算是否已被抬高」属策略层职责（P2），
Repository 只负责按这个布尔执行状态机约束（默认 `False` = 失败关闭）。

## 四之二、`executions` 缓存列的折叠规则

`executions` 的 `state` / `error_code` / `output_ref` **都是缓存**，真相是 `execution_events`。
三列的折叠规则必须与写入侧严格同构，否则 `verify_consistency()` 会自相矛盾：

| 列 | 折叠规则 |
|---|---|
| `state` | 逐个 `apply()` |
| `error_code` | **最后一个非空值胜出**（= 最近一次出现的错误码，不是"导致终止的错误码"） |
| `output_ref` | **只有进入 `COMPLETED` 的事件**的 `payload_ref` 才算交付指针；其余终态与中途步骤的 `payload_ref` 都不上浮 |

### `output_ref` 为什么只认 `COMPLETED`

五个终态里只有 `COMPLETED` 有"交付"语义。`FAILED` / `TIMEOUT` / `ABORTED` /
`BUDGET_EXCEEDED` 是被打断的，它们事件上可能挂的 `payload_ref`（错误栈、诊断快照）
是**诊断产物**，不是交付物。

两类东西混进同一列会让 Replay 分不清"这个执行交付了什么"与"它留下了什么现场"。
诊断产物不需要新字段 —— 它本来就在事件行里，`list_events()` 逐条取即可。
因此取舍是：`output_ref` 只回答"交付了什么"，回答不了就是 `null`。

关于 `error_code` 为什么不在仓库层收紧成"只有终态可写"：`execution-event.schema.json`
允许**任意**事件携带 `error_code`（L2 语义收紧是校验层的职责，不是存储层的）。
若在此处硬性禁止，就会造出 schema 与实现的第二种不一致 —— 与"schema 允许、
仓库层拒绝"这种矛盾相比，"最后一个非空值胜出"这条命名规则更诚实。
（`output_ref` 不适用这条推理：它不是"允许任意事件携带"的字段，而是仓库自己定义的缓存列。）

`output_ref` 原先是一个独立的 `append_event` 入参，但它只写进列、不进事件流，
于是那列**无法从事件流重建**，`verify_consistency()` 也验不了它，与"事件流是真相"直接冲突。
现在交付指针只有一个来源：终态事件的 `payload_ref`。

## 四之三、读写边界（实现约束）

- **写**：只有 `append_event()` 能推进状态，一律 `BEGIN IMMEDIATE`。
  非法转移在**任何写之前**就抛出，因此"不落半行、状态原样"成立。
- **读**：**所有**公开读方法都走 `_read()`（持锁 + `BEGIN DEFERRED`，
  在 `in_transaction` 时零开销复用外层事务）。原因不是"越多事务越好"，而是
  「先读 `seq`、再读事件流」这类多语句读必须落在同一个快照上，
  否则并发提交会被 `verify_consistency()` 误报成数据损坏。
  单语句读也一并走同一条路 —— 这样"公共读者不直连 `self._conn`"是一条**统一规则**，
  不依赖每个方法的作者记得自己是单语句。该规则由源码扫描测试锁死。
- **回滚**尽力而为：`_rollback_quietly()` 先查 `in_transaction` 再回滚，并吞掉
  `sqlite3.Error`。裸 `ROLLBACK` 在事务已被外部弄没时会抛
  `cannot rollback - no transaction is active`，**把真正的根因盖掉**。

## 四之四、`step` 字段的语义

事件上的 `step` **只在 `engine_step_ended` 上必填**，含义是"**刚收尾的步骤**"
（不是"即将开始的步骤"）。其余事件不带 `step`（`engine_started` 没有步骤在跑，
`engine_completed` 是整个流水线的完成，不归属某一步）。

这条约定是内核游标推导的依据：**当前步骤 = 最后一个 `engine_step_ended` 的 `step` 的下一步**。
于是「跑到第几步」是事件流的函数，不是内存状态 —— 进程重启、或换一个调度器接手，
看到的都是同一步骤，也不会出现"内存游标飘了但事件流没飘"的分叉。

六步的权威顺序见 `validator/pipeline.py::PIPELINE_STEPS`，
它与三处 schema 枚举（`workflow.schema.json` 的 `pipeline` / `step_overrides` 键、
`execution-event.schema.json` 的 `$defs.step`）由 `tests/test_pipeline.py` 锁死一致。

## 五、与其他层的边界

| 谁 | 能做什么 | 不能做什么 |
|---|---|---|
| Stillroom Engine | 发 `engine_*` 事件、推进步骤 | —— |
| `abort_execution` / `resume_execution` | 只发 `abort_requested` / `resume_requested` | 不能指定目标状态 |
| Agent / MCP 客户端 | 读状态、发这两个工具调用 | **不能直接写状态**，也不能提升 trust level |

状态机的输入是「状态 + 事件」，输出是「新状态 + 错误码」；
幂等（`request_id` → `execution_id` 永久绑定）与超时计时都在 Repository / Engine 侧，
不在本模块内 —— 这样本模块可以纯离线单测。
