# 版本记录

本项目遵循「日期 + 语义化版本」。版本号只在**产品层面有意义的节点**递增
（更名、对外发布、能力增减），日常修复不单独占版本号。

**本文件是项目级 CHANGELOG**：所有模块（GUI 桌面 / 无头协议层 / 打包 / 文档）的版本节点
**混在一起记录，不按模块拆分文件**。便于按模块检索的做法是——每个节点在标题里点名**涉及面**
（如 `v1.1.0` = 无头层、GUI 侧的节点 = GUI 接线），版本号**全局递增、跨模块连续**，这样
「先无头后 GUI」或二者交替推进都能用一条版本线表达。

## v1.2.0 — 2026-10-02 · 助手页「对话」（GUI 接线）

**与 P1 无头层（v1.1.0）是两个独立节点，并行开发。** 本节点把桌面端助手页接上协议层：
「对话」的数据**只走** `app/adapters/protocol_client.py`，**界面不直接碰协议**。

改动（落地时间 2026-09-29 ~ 09-30）：

| 类型 | 文件 |
|---|---|
| 修改 | `app/main.py`、`app/ui/qml/Main.qml`、`app/ui/qml/components/AppButton.qml`、`app/ui/qml/pages/AgentPage.qml` |
| 新增 | `app/ui/assistant_bridge.py`、`app/ui/pseudo_stream.py`、`app/adapters/`（`protocol_client.py` + `__init__.py`）、`app/ui/qml/components/{ChatPanel,ChatMessage,MessageActionButton}.qml` |
| 新增测试 | `tests/test_assistant_page.py`、`tests/test_chat_ux.py`、`tests/test_qt_fonts.py` |

**与 P1 的边界**：本节点**不改动无头层**（`validator/` `runtime/` `mcp_server/` `schemas/`
`contracts/`），只**消费**它的接口 —— 正是原始设计里的目标状态。反向约束见
[docs/P1-实施计划.md](docs/P1-实施计划.md) §四。

> ⚠️ **本机未验证**：上述三个新增测试模块依赖 `PySide6`，而本机 `PySide6.QtCore` DLL
> 加载失败 → 采集即 `Interrupted`。**本节点的验证需在能跑 GUI 的环境完成。**

## v1.1.0 — 2026-10-02 · P1 协议 v1.0：Agent-to-Service 无头服务

**这是一个能力节点，不改产品外观。** 新增三个包（P1 无头层）+ 三份契约 + 三份 schema，
把 Stillroom 从「给人用的桌面工具」扩出一条「给 Agent 用」的通道：上层 Agent 走 MCP
连进来，把 Stillroom 当**工作流注册中心 + 路由执行引擎**使用 —— Agent 只能操作
Workflow Definition 与 Execution，**永远不能直接碰 Kernel**。

**无头层不 import `app/`**（由 `test_headless_boundary.py` 的 A1.2 源码扫描强制）。

> ⚠️ **验收状态是「部分验收」，不是全绿收口。** 缺口全部来自本机 PySide6 环境，
> 与 P1 改动无关（受影响文件均**不在 P1 交付范围内**）。详见下方「验收状态」与「已知限制」。

### 交付内容

依赖只向下：`mcp_server/ → runtime/ → validator/ → schemas/`。

| 区域 | 内容 |
|---|---|
| `validator/` | `state_machine.py`（**9 态**执行状态机：`PENDING` `RUNNING` `WAITING_INPUT` `ABORT_PENDING` `COMPLETED` `FAILED` `TIMEOUT` `ABORTED` `BUDGET_EXCEEDED`）、`errors.py`（`ErrorCode` 单一来源）、`workflow_validator.py` / `skill_validator.py` / `pipeline.py`（四层校验：Schema → Semantic → Trust/Policy → Metadata） |
| `runtime/` | `repository.py`（`ExecutionRepository`：追加式事件流 + 所有公开读走 `_read()` 持锁读）、`registry.py`（`WorkflowRegistry`）、`artifacts.py`、`stub_kernel.py`（P1 的确定性 scheduler）、`router.py`（LLM rerank 只留注入点）、`hashing.py` / `schema.py` / `base.py` / `errors.py` |
| `mcp_server/` | `jsonrpc.py`（分帧 / 解析）、`dispatch.py`（方法名精确路由）、`mcp.py`（**唯一**线协议适配层）、`tools.py`（**11 个工具** + 信封）、`identity.py`（三预设身份 `agent` / `human` / `system`，**唯一**来自进程启动配置）、`stdio.py`（`build_server_context` / `run` / `close_quietly` **三段分离**）、`__main__.py`（入口，包内**唯一**引用标准流的模块） |
| `schemas/` | `workflow.schema.json` · `execution-event.schema.json` · `artifact-manifest.schema.json` |
| `contracts/` | `execution-state-machine.md` · `mcp-tools.md` · `persistence.md` |

### 线协议（接入方最需要知道的一层）

- **线协议上是 3 个 MCP 方法**：`initialize` / `tools/list` / `tools/call`，换行分隔
  JSON-RPC 2.0 over stdio（**无** `Content-Length` 头）。**11 个工具名只作为
  `tools/call` 的 `params.name` 存在**，不是方法名 —— 否则任何标准 MCP 客户端都连不上。
- **`-32602` 一律带 `error.data.reason`**（四值枚举 `invalid_name` / `unknown_tool` /
  `invalid_arguments` / `unexpected_param`），客户端**按 reason 分支，不匹配报错文案**。
  注意分层：`arguments` **内部**字段缺失属**业务层**（`INPUT_SCHEMA_INVALID` → `result`
  里的 `isError`），**不是** `-32602`。
- **`isError` 判据是 `result.get("ok") is not True`** —— 身份判断，不是真值判断
  （`1 == True` 但 `1 is True` 为假）。**业务错误绝不走 `error` 字段**，否则客户端
  分不清「方法不存在」与「参数写错了」。
- **stdout 纪律拆两条**：规则 A「不**写** stdout」覆盖包内所有模块；规则 B「不**引用**
  `sys.stdin` / `sys.stdout`」只豁免**唯一**入口 `mcp_server.ENTRY_MODULE`
  （`= "mcp_server.__main__"`），该集合由测试锁成**结构事实**。

### 四条不变式（P1 的硬约束）

1. 六步流水线**显式声明**，每步声明 `cancellable`；
2. T0–T3 权限 = creator 能力 ∩ workflow 声明 ∩ system policy；
3. metadata 只描述、**不改变执行**；
4. `request_id` **永久绑定**首次 `execution_id`；`ABORT_PENDING` **唯一出口**是
   `engine_step_ended → ABORTED`；`retry_execution` **无 override**。

19 条设计决策逐条记录在 [docs/P1-实施计划.md](docs/P1-实施计划.md) §八。

### 协议 v1.0 冻结声明

**`schemas/`、`contracts/`、`validator/` 三个目录自本节点起视为冻结（frozen）。**

| 变更类型 | 例 | 处理 |
|---|---|---|
| **兼容性变更** | 新增可选字段、新增工具、新增错误码取值 | 递增**次版本号**，契约里标注 `since` |
| **不兼容变更** | 改字段语义、改状态迁移、改错误码取值、删字段 | **必须**发新协议版本（v1.1 / v2.0），并走 **deprecation 周期**：旧行为至少保留一个次版本，CHANGELOG 写明「何时废弃、何时移除」 |

**「发新版本」的具体形态**：P1 冻结的 `schema_version` 为 **`"1.0"`** —— 它是**每份文档里的一个字段**，三份 schema 均以 `"const": "1.0"` 钉死（`workflow` / `execution-event` / `artifact-manifest`）。发生**不兼容变更**时，`schema_version` 升到 `"2.0"`，并在 `schemas/` 让**旧新并存**：`workflow.schema.json`（v1）与 `workflow.v2.schema.json`（v2）同时存在，`contracts/` 同理（`mcp-tools.md` 与 `mcp-tools.v2.md`）。旧文件在 deprecation 期内保持可用、不再演进 —— 避免「直接原地改 v1 文件」把老客户端打哑。

本阶段 `schemas/` 与 `contracts/` **只增不改**（A11）—— 现有契约文件全部为**新增**，
无既存文件被改写。同时 P1 的冻结依赖外层 `sqlite` schema 版本（`schema_meta`），
库 schema 比代码新时以 `RepositoryError(SCHEMA_INVALID)` **拒绝启动**。

### 验收状态：部分验收

| # | 断言 | 断言位置 | 状态 |
|---|---|---|---|
| A1 | 无头边界（不加载 PySide6 / 不 import `app.*`） | `tests/test_headless_boundary.py`（A1.1–A1.5，**14 项**） | ✅ |
| A2 | 11 个工具成功路径 + ≥2 条失败路径 | `tests/test_mcp_tools.py`（**88 项**） | ✅ |
| A3 | 错误码单一来源（`code ∈ ErrorCode`，禁字面量） | 同上 | ✅ |
| A4 | 幂等（同 `request_id` → 同 `execution_id`，DB 只落一行） | `tests/test_idempotency.py`（**18 项**） | ✅ |
| A5 | 状态可重放（事件流重放 ≡ 当前状态） | `tests/test_repository.py`（**33 项**） | ✅ |
| A6 | 中止两段式（`ABORT_PENDING` 无第二出口） | `tests/test_stub_kernel.py`（**15 项**）+ `tests/test_state_machine.py`（**89 项**） | ✅ |
| A7 | retry 无 override + `parent_execution_id` + 复用 `request_id` | `tests/test_idempotency.py` | ✅ |
| A8 | `create_workflow` 四层校验（11 个 fixture） | `tests/test_mcp_tools.py` | ✅ |
| A9 | 端到端链路（A9.1–A9.9） | `tests/test_mcp_server_e2e.py`（**15 项**） | ✅ |
| A10 | 零回归：全量 ≥ 654 项**全绿** | `pytest` 全量 | ⚠️ **部分验收** |
| A11 | 契约不回退（`schemas/` / `contracts/` 只增不改） | `git diff` + 本文件 | ✅ |

**A10 为什么是「部分验收」**：本机全量实测 **936 passed / 12 failed / 9 collection errors**。
- **数量口径满足**：936 ≥ 654。P1 新增测试分布在 `test_mcp_jsonrpc`(73) ·
  `test_mcp_dispatch`(33) · `test_mcp_methods`(55) · `test_mcp_stdio`(45) ·
  `test_protocol_schemas`(8) · `test_mcp_tools`(88) · `test_repository*`(33+15+4=52) ·
  `test_schema_meta`(13) · `test_idempotency`(18) · `test_artifacts`(26) ·
  `test_stub_kernel`(15) · `test_workflow_registry`(29) · `test_workflow_validator`(14) ·
  `test_headless_boundary`(14) · `test_mcp_server_e2e`(15) 等。
- **「全绿」这一半本机拿不到**：**12 项失败 + 9 个采集失败模块全部由
  `PySide6.QtCore` / `QtGui` DLL 加载失败引起**（见下节），其中 9 个纯 GUI 模块
  连 `pytest` 采集阶段都过不去（默认直接 `Interrupted`）。
- **排除 Qt 依赖后的无头子集：929 passed / 0 failed，全绿。**

### 已知限制（本机环境，非 P1 缺陷）

**`PySide6` DLL 加载失败**：`ImportError: DLL load failed while importing QtCore: 找不到指定的程序`。

| 类别 | 清单 | 表现 |
|---|---|---|
| 采集即失败的 **9 个模块** | `test_agent_bridge` `test_assistant_page` `test_chat_ux` `test_history_model` `test_knowledge_bridge` `test_prompt_patches` `test_qt_fonts` `test_settings_bridge` `test_ui` | `pytest` 在采集阶段 `Interrupted`，须加 `--continue-on-collection-errors` 才能看到其余用例 |
| 运行期失败的 **12 个用例** | `test_media_thumb`(4) · `test_vision_client`(7) · `test_registry`(1) | 图缩放走 Qt（内部用 `QImage`），Qt 起不来时**静默返回 `None`** → 被测代码报「图片无法解码」 |

**可执行判据**（区分「环境问题」与「真回归」）：

```python
from PIL import Image
Image.open(png).load()                              # 本机：PIL 12.3.0 正常返回 —— 图没坏
from app.services.media import jpeg_bytes_scaled
jpeg_bytes_scaled(png)                              # 本机：返回 None —— Qt 起不来（内部走 QImage）
```

同一张 PNG：**PIL 能解、`jpeg_bytes_scaled` 返回 `None`** ⇒ 是 Qt 起不来，**不是图坏**。
**若在有 GUI 的环境复跑这 12 项仍失败，则说明是 P1 无头层的真实回归，而非环境问题。**
受影响文件均**不在 P1 交付范围内**（P1 无头层不碰 `app/`，见 §四）。
**A10 的「全绿」这一半需在能跑 PySide6 的环境复验。**

### 测试与反证

- **无头子集**：`929 passed / 0 failed`（`pytest -o addopts="" -q`，排除 Qt 依赖文件后）。
- **反证（变异测试）**：`tools/prove_mcp_server.py` 对 `mcp_server/` 与
  `app/adapters/protocol_client.py` 施加 **27 个定点变异**，断言对应用例必须变红
  —— **27/27 全部被抓住**。它守的是「测试全绿只说明当前实现没被抓住」这个盲区。
  > **本机必须分批跑**（WorkBuddy safe-delete 的**按工具调用累计**删除守卫，阈值 50）：
  > 一次调用里跑 ~60 次 pytest 会越线，越过之后每次删除都抛 `SystemExit`，表现为
  > 「前 N 条全过、接着十几条被记成 error」——**一条用例都没坏**，是守卫掐掉了进程。
  > 按场景名分批（每批 ~4 个），分 **8 批**跑完。详见 [docs/P1-实施计划.md](docs/P1-实施计划.md) §三 WP4。

### 明确不做（P1 边界，越界先记 CHANGELOG 再谈）

真实六步流水线（P3）· Router 真实 LLM rerank（P2）· Replay 界面（P3.5）·
Workflow Factory（P4）· `create_skill` 只登记元数据**永不执行** · 沙箱隔离的真实实现（P3）。

## v1.0.0 — 2026-09-26 · 更名为 Stillroom

### 改名（这是本次的核心变更）

**从 `Agnes Studio` 改为 `Stillroom`。** 两个理由，第二个比第一个更重要：

1. **原名本来就是拼写错误**。目录与 exe 名写成 `Agens`，但代码里 `Agnes` 出现
   **135 次**、`Agens` 只有 **11 次**；平台官方域名是 `agnes-ai.com` / `agnes-ai.cn`。
   `Agens` 这个拼写既不是平台名，也不是任何英文词——是建目录时的手误，
   后来被 exe 名和 `pyproject.toml` 沿用。
2. **即使拼对了也不该叫 Agnes**。这个工具是**多供应商**的：Agnes 出图出视频、
   DeepSeek / 智谱 看图、Jev 做判断、Tavily 联网。叫平台名会把自己框成
   「Agnes 的客户端」，而它其实是一层编排。

**为什么叫 Stillroom**：蒸馏室——把原料蒸馏提纯。对应这个工具做的事：
把一句模糊需求经过「判断 + 检索 + 精炼」得到可用产物。
选名时用 GitHub 搜索 API 逐个查过占用（`q=<name> in:name`）：

| 候选 | 同名仓库数 | 结论 |
|---|---|---|
| **Stillroom** | **40** | 选用（候选里最少） |
| Moraine | 93 | 备选 |
| Cairn | 1890 | 备选（无同类工具） |
| Kiln / Easel / Anvil / Loft / Loom / Bench | 1795 ~ 146628 | **都有数千星的同名项目，不用** |

### 兼容性（重要）

**产品名改了，凭据与既有路径名一个都没改**——改了会丢配置：

| 保持不变 | 原因 |
|---|---|
| 环境变量 `AGNES_API_KEY` / `AGNES_BASE_URL` 等 | 那是**平台凭据**的名字，改了连不上 |
| `%APPDATA%\AgnesGenerator\settings.json` | 老用户的偏好文件还在这里 |
| 默认数据目录 `F:\AgnesGeneratorData` | 历史、知识库、缓存都在里面 |
| `AGNES_*` 内部开关（如 `AGNES_WITH_VIDEO`） | 打包与测试脚本在用 |

**只有产品层面的名字变了**：exe 名（`Stillroom.exe`）、窗口标题、应用名、
保存文件的默认前缀（`stillroom_<id>.png`）、`pyproject` 里的包名。

升级方式：把 `dist/Stillroom/` 换掉即可，`.env` 与数据目录原样沿用。

### 本版同时带上的修复（更名前夜撞出来的）

| 修复 | 说明 |
|---|---|
| 助手页结果卡补「另存为」 | 手动页与历史灯箱有、**助手页漏了**，导致助手出的片子存不下来 |
| 历史灯箱补「详情」 | 能看到**平台任务 ID** 与**失败原因全文**（此前只显示「失败」，事后没法对账） |
| 知识库切片剥掉开头标题 | 否则 md 的 `##` 会混进提示词被模型当正文读 |
| 图片页补「+ URL」入口 | 那句提示原本就写着「也可以直接填公网 URL」，但**没有输入框** |
| 提示词实时字数 | 图片页与视频页的提示词卡片右上角 |
| 切片剥标题（同上）与迁移遗漏审计 | 见 [docs/迁移遗漏审计.md](docs/迁移遗漏审计.md) |

### 新增

- **`tools/import_prompt_library.py`**：把开源提示词库（标准 JSON）转成可入库的
  Markdown。实测对比：JSON 直接入库是 513 片、检索「赛博朋克霓虹」0 命中；
  转成 Markdown 后 92 片、命中 1 条。规则与理由都写在脚本注释里。
- **`README.md`**：含与 [infinite-canvas](https://github.com/basketikun/infinite-canvas)
  的逐项对照。
- 文档：[docs/方案-更名与借鉴无限画布.md](docs/方案-更名与借鉴无限画布.md)

### 许可

采用 [PolyForm Noncommercial License 1.0.0](LICENSE)（非商业许可）。

**为什么不是 MIT**：MIT 明文允许商用（条款里含 "sell"），而本项目希望
「源码敞开给你看和学，但不许拿它赚钱」。两者不能同时成立，所以选了 PolyForm
Noncommercial —— 它是正经的开源许可证（不是自造条款），明确允许个人学习、
研究、实验、业余项目，商业用途不在许可范围内。

**Required Notice**：`Copyright zzq (https://github.com/q93304989-bit)`
（PolyForm 要求分发时一并带上这行）

### 公开仓库与同步方式（方案 B）

代码公开在 https://github.com/q93304989-bit/stillroom —— `main` 是发版状态，
`develop` 是同步目标；`docs/` 与 `.idea/` **不公开**。

**为什么不是直接推**：`docs/` 散在二十多个提交里，直接推历史会把它们一起带
出去；而且公开仓库只要一份干净的代码快照。所以定成「本地正常开发，需要时用
脚本同步一次」。

工具是 **`tools/publish.py`**：本地 commit 之后跑一次，它把「已跟踪的全部文件」
（排除 `docs/`、`.idea/`）导出到 `.publish/` 再推 `develop`。规则写死在脚本里，
避免手工挑文件那种「改了一处、漏了另一处」：

- 推送前**必扫密钥**（`.env` 绝不能被带上），有命中就中止，不提交不推送；
- 没改动就不造空提交；导出用 `git archive HEAD`，所以未跟踪的临时文件不会被带上；
- 本地 `.gitignore` 不能忽略 `docs/`（本地要跟踪它），脚本按 `EXCLUDED` 给公开
  仓库补一块额外忽略（幂等）。

用法见 `docs/教程-同步到公开仓库.md`（`docs/` 不随仓库公开，这行链接只在本地有效）。

## v0.1.0 — 2026-09-21 ~ 2026-09-26 · 能力建设期（原名 Agnes Studio）

这 6 天的 45 笔提交，按主题归并：

### 助手流水线（9-21 ~ 9-22）

- 六步流水线：理解需求 → 找参考 → 生成 → 评估 → 精修 → 交付
- 判断交给 Jev（够不够开工 / 这张行不行 / 该改哪里），动作交给能力注册表
- 工具闸门与验收：参数校验 → 预算 → 审批 → 循环检测 → 限流 → 结果验收
- A-RAG 第一版（从自己的历史作品里找参考）+ 助手页面

### 可编辑上下文与知识库（9-23 ~ 9-24）

- **第一期 可编辑上下文**：先出草稿、来源开关、条目增删、用户裁决是硬约束
- **第二期 知识库**：上传 → 切片 → 入库 → 检索；知识库页面
- **第三期 联网混合检索**：阈值三层取最高、本地不够才联网、provider 可插拔
- **第四期 收尾**：长期档案、跑完「改参考再跑一次」

### 视觉与稳定性（9-24 ~ 9-25）

- 界面截图工具（含上下文草稿、知识库页）
- 三处必然触发的自身缺陷修复：轮询被循环检测误杀 / 视频无法自动评估 /
  503 重试吞掉额度（详见 [docs/Agent-实施计划.md](docs/Agent-实施计划.md) 对应小节）

### 迁移遗漏与补齐（9-25 ~ 9-26）

重写成 Qt Quick 时漏搬的功能逐条补回：「另存为」、提示词字数、详情（任务 ID /
失败原因）、图片页 URL 入口。排查方法与清单见
[docs/迁移遗漏审计.md](docs/迁移遗漏审计.md)。
