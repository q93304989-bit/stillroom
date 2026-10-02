# Persistence — 协议库的物理结构（v1.0）

> 契约文件。实现见 `runtime/schema.py`（建表/迁移）、`runtime/base.py`（事务纪律）、
> `runtime/repository.py`（协议域）、`runtime/registry.py`（注册域）、
> `runtime/artifacts.py`（内容寻址）。
> 不变式由 `tests/test_schema_meta.py` / `test_artifacts.py` / `test_workflow_registry.py` 锁死。

## 一、一个物理库，多个逻辑域

`protocol.db` 里住着三类**独立演化**的东西，各占一个 `scope`：

| scope | 表 | 演化动力 | 当前版本 |
|---|---|---|---|
| `protocol` | `executions` / `execution_events` | 随 Agent↔Stillroom 协议演化 | 2 |
| `registry` | `workflows` / `skills` | 随业务与工作流能力演化 | 1 |
| `bindings` | `request_bindings` | 幂等锚点，基本不动 | 1 |

各域在 `schema_meta(scope, version, migrated_at)` 里**各存一份版本**，迁移决策按域判断。
`PRAGMA user_version` 保留为 `protocol` 域的兼容镜像，同时是老库的迁移起点
（老库没有 `schema_meta`，只能从 `user_version` 与"表在不在"推出当前版本）。

**为什么不用一个整数管全库**：任何一处的迁移都会强迫另外两处"跟上版本号"，
迁移脚本立刻退化成 `if version >= 2 and table_x_has_column_y` 那种靠表结构反推的分支 ——
去查 `PRAGMA table_info` 兜底，等于承认版本表不可信。

**`migrated_at` 只在真的动过时更新**（首次登记，或版本推进）。每次打开都无条件写回的话，
它的含义就从"这个域上次迁移是什么时候"退化成"上次谁打开过库"，诊断价值归零。

## 二、分域版本各自独立的保证

`ensure_schema()` 对每个域算 `current → target`，只跑差的那些步骤，
并且**只写回真的变过的域**。所以：

- 只缺注册域时，打开库不会碰协议域的版本号与 `migrated_at`；
- 迁移包含建表时**整体在一个 `BEGIN IMMEDIATE` 里**，失败则连 `schema_meta` 一起回滚 ——
  不会留下"版本表已建、表却没升"的半成品；
- 某域版本高于代码支持时抛 `SCHEMA_INVALID`，不做"尽力兼容"。

**迁移只跑一次**由版本表决定，不做表结构探测。若版本门失效、`ALTER TABLE ADD COLUMN` 被重跑，
SQLite 会抛 `duplicate column name` —— 所以"能开第二次"本身就是证据。

## 三、两个域类：共用 `db_path`，互不依赖

```python
ExecutionRepository(db_path)      # 协议域 + 幂等域
WorkflowRegistry(db_path, artifacts=None)   # 注册域
```

- **各开自己的连接**，所以域之间**没有跨域事务**。这是有意的：
  工作流定义是长期资产，"注册成功但这次执行失败"正是期望结果，不该被一起回滚。
- `ExecutionRepository` **不持有** `WorkflowRegistry` 引用，它只被喂
  `workflow_id + workflow_version + definition_hash + definition_ref` 四个入参。
  将来要物理分离，改动只是给某个域换 `db_path`，而不是"从一个大类里剥出半张表"。
- 连接配置、锁、`_read`/`_write` 的快照边界**只有一份**（`runtime/base.py::DomainDatabase`）。
  那段是全项目最脆的代码，存两份 = 迟早只修好一份。

### 读写边界（继承者必须遵守）

- **写**一律 `BEGIN IMMEDIATE`（拿写锁，跨连接串行化写者）。
- **读**一律走 `_read()`：持锁 + `BEGIN DEFERRED`，`in_transaction` 时**零开销复用**外层事务。
  多语句读必须落在同一个快照上，否则"先读 A 再读 B"会看到两个时点，
  并发写入会被自检误报成数据损坏。
- 单语句读**也**走 `_read()`：于是"公共读者不直连 `self._conn`"是一条统一规则，
  不靠每个方法的作者记得自己是单语句。由源码扫描测试锁死。

## 四、`executions` 的三列都是受校验缓存

见 `contracts/execution-state-machine.md` §四之二。这里只补一句新列：

| 列 | 来源 |
|---|---|
| `workflow_definition_hash` | **被喂进来的**（调用方从 registry 取），不是每次读去问 registry |
| `workflow_definition_ref` | 同上 |

钉住它们的理由：`workflow_version` 只是版本号，**版本号不等于内容**。
将来 registry 的 active 版本变了、或历史数据不干净，靠这两列仍能精确回答
"这个 execution 跑的是哪一版定义"。`retry()` **继承**这两列 ——
否则"重试"会变成"跑新定义"。

### 谁负责填（P1 的落点，别猜）

`ExecutionRepository.bind_request()` 签名里已有这两个可选参数：

```python
bind_request(*, request_id, workflow_id, workflow_version,
             input_snapshot=None, trust_level=None,
             workflow_definition_hash=None, workflow_definition_ref=None) -> BindResult
```

填充路径是 **调用方喂数据**，不是仓库回查：

1. MCP Server 层的 `execute_workflow`：先 `registry.require_active(workflow_id, version)`
   拿到 `WorkflowRecord`，再把 `record.definition_hash` / `record.definition_ref`
   传进 `bind_request()`。**`ExecutionRepository` 始终不持有 registry 引用** ——
   这与"两域互不依赖"是同一条约束，不是两件事。
2. 直接调用仓库的测试/工具（P1 的多数用例）**可以不传**，两列落 `NULL`。

**不变式：两列必须同时为 NULL 或同时非 NULL。** 只填一个等于半个指纹，
将来无法判断"这个 execution 跑的是哪一版定义"。

> ⚠️ 因此**不要**写"execution 的定义指纹必须非空"这类断言。
> `NULL` 是被允许的状态，见 `test_an_execution_without_a_pinned_definition_is_still_valid`。
> 要断言就断言"过了 MCP Server 的 `execute_workflow` 之后非空"。

## 五、工作流定义内容不进 SQLite

`workflows` 行里只有 `definition_hash`（内容指纹）+ `definition_ref`
（内容地址 `artifact:sha256:<64 位裸 hex>`）。定义本体落在内容寻址的 blob store：

```
<root>/sha256/<前两位>/<完整 hex>        # root 默认在 protocol.db 旁边
```

### 两条硬性质

1. **内容寻址**：同样的字节永远同一个地址；重复写入是空操作（幂等）。
2. **读时校验**：取回时重算 hash 并比对，对不上就炸。
   不强制这条，"地址是内容的函数"就只是口号，磁盘损坏会静默给出错内容。

落盘走"同目录临时文件 + `os.replace`"，避免读到写了一半的内容。
`put_json` 落的是**规范化 JSON**（键排序、紧凑分隔符、不转义非 ASCII），
所以键序不影响地址。

### 为什么不为工作流定义伪造 Artifact Manifest

`schemas/artifact-manifest.schema.json` 的 `required` 里有 `execution_id` 与 `step`（六步枚举之一）。
工作流定义**不属于任何一次执行、也不属于任何一步** —— 硬塞一个 `execution_id`
只会把"这是执行产物"这件事说谎。分工是：

| 东西 | 存法 |
|---|---|
| 任意内容（工作流定义、将来的中间产物） | blob store：`sha256 → 文件` |
| 执行产物的**清单**（谁产的、哪一步、多大） | 由 `artifact-manifest.schema.json` 描述，属评估/交付链 |

### `(workflow_id, version)` 内容不可变

同一 `(workflow_id, version)`：内容一模一样 → 幂等返回既有记录（不重复写盘）；
内容不同 → `WORKFLOW_VERSION_IMMUTABLE`。版本号一旦发布就绑死内容，
否则"某 execution 跑的是 v3"这句话没有意义。

注意 `version` 本身是定义的一部分，所以 v1 与 v2 的 `definition_hash` **不同**。

### 状态机（三态）

```
pending_activation --activate()--> active --deprecate()--> deprecated
```

- `activate()` 就是契约里那道**人工激活**闸门。`activation: "auto"` 且 creator 达标
  （`max_trust_level` 达 T3 且 `can_auto_activate`）时，`register()` 直接落 `active` ——
  这条判定在 L3 校验里，`register()` 只是照抄结论。
- `deprecated` 让工作流能"下架而不删除"，历史 execution 仍能靠
  `(workflow_id, version)` 找回它跑过的定义。
- 刻意**没有** `draft` / `tested`：`contracts/mcp-tools.md` 的 `create_workflow`
  只定义了 `pending_activation | active`（新建的定义不可能是 `deprecated`），
  多两个状态就是多两处要同步的语义。

## 六、四层校验是落库闸门

`register()` 先过 `validate_workflow()`，**失败则完全不落库** —— 没有行，也没有 blob。
只写盘不写行（或反过来）都是半个状态，将来按 ref 读会拿到孤儿内容。

`get_definition()` 读回时做**第二道**校验：store 保证"文件内容 == 地址"，
registry 再保证"地址 == 登记时记下的指纹"。少了后者，改一行 SQL 就能让
`get_definition()` 交出另一份定义而没人发现。

## 七、错误码

一律取自 `validator/errors.py::ErrorCode`，不另造字符串。本契约相关：

| 场景 | 错误码 |
|---|---|
| 某域 schema 版本高于代码 / 内容与指纹对不上 | `SCHEMA_INVALID` |
| 定义未过 L1 | `SCHEMA_INVALID`（取 `ValidationIssue.code`） |
| 定义未过 L2 / 非法状态迁移 | `SEMANTIC_INVALID` |
| 定义未过 L3 | `INSUFFICIENT_TRUST` / `CAPABILITY_DENIED` |
| 定义未过 L4 | `METADATA_FORBIDDEN` |
| 工作流版本不存在 | `WORKFLOW_NOT_FOUND` |
| 工作流存在但未激活 | `WORKFLOW_NOT_ACTIVE` |
| 同版本不同内容 | `WORKFLOW_VERSION_IMMUTABLE` |
| blob 缺失 | `EXECUTION_NOT_FOUND` |
