# Stillroom | 无限画布让你编排每一步，Stillroom 让你说完一句自己跑完。

> 判断优先的本地多模态生成工作台：你只说一句需求，**代码**跑完六步流水线，
> 模型只回答窄问题。图片 / 视频、历史检索、知识库、联网兜底都在本地完成。

Windows 桌面应用（PySide6 + Qt Quick），onedir 绿色版，双击即用。
密钥在 exe 同级的 `.env`，数据在本机 SQLite —— **不出本机、不需要服务器**。

> **改名说明**：本项目原名 `Agnes Studio`（更早的目录名还把 Agnes 误拼成 Agens）。
> 2026-09-26 更名为 **Stillroom**，原因见 [CHANGELOG.md](CHANGELOG.md)。
> 凭据键名（`AGNES_*`）与数据目录（`%APPDATA%\AgnesGenerator`）**保持不变**，老配置照旧可用。

## 它和「无限画布」有什么不同

先说结论：两者都做 AI 出图出视频，但**重心不同** ——
[infinite-canvas](https://github.com/basketikun/infinite-canvas) 是**画布优先**，
本项目是**判断优先**。不是谁替代谁。

| | 无限画布 | Stillroom |
|---|---|---|
| 一句话 | 你在无限画布上摆节点、连线，**手动编排**创作流程 | 你只说一句需求，**代码**执行六步流水线 |
| 交互核心 | 拖拽、连线、缩放、小地图 | 说完 → 看草稿 → 改参考 → 确认 → 跑 |
| 谁决定顺序 | **你**（连什么线就走什么路） | **代码**（阶段顺序写死，模型改不了） |
| 形态 | 浏览器 Web 应用，Docker / Render 部署 | **桌面 exe**，双击运行 |
| 数据 | 浏览器 IndexedDB，可选 WebDAV 同步 | 本机 `history.db`（SQLite）+ 文件目录 |
| 模型角色 | 主要出图出视频；Agent 可操作画布 | 只回答窄问题（够不够开工 / 这张行不行 / 该改哪里） |
| 参考来源 | 提示词库（7 个在线源）+ 画布素材 | 历史作品（A-RAG）+ 用户知识库 + 联网，**三层阈值** |
| 生成后评估 | 没有自动评估（靠人看） | 视觉模型描述 → Jev 打分 → 达标 / 精修 / 请示 |
| 自我改进 | 无 | 提示词补丁：提议 → 接受 → 版本化 → 可回滚 |
| 可审计 | 节点上记录参数 | 每次运行的上下文快照 + 调用账目 + 阶段留痕 |
| 额度控制 | 未强调 | 预算闸门 + 限流 + 循环检测 + 重试免额度 |
| 插件 / 多 Agent | **插件系统 + TS SDK、多 Agent 协同**（明显更强） | 12 项能力为闭集，无插件 |
| 成熟度 | 7000+ star、社区与赞助商 | 个人项目、6 天 45 笔提交 |

**各自适合谁**

- 要手动摆布、反复试同一张图的多种构图 → **它更顺手**（画布本来就是干这个的）。
- 只想说一句、要它自己找参考跑完、还想事后说得清「为什么这么跑」 → **Stillroom 更合适**。

## 核心能力

### 六步流水线（代码掌控顺序）

```
理解需求 → 找参考 → 生成 → 评估 → 精修 → 交付
   ↑                    ↑      ↑
   └ 判断链（Jev）      └ 预算/白名单/审批/限流都在这一层强制生效
```

- **流程在代码里**：阶段顺序、回修上限（2 轮）、什么条件算通过，全是代码写死的。
- **模型只做窄判断**：六问 —— 任务类型 / 可行性 / 信息够不够 / 画幅 /
  要几条参考 / 有没有点名联网。
- **一切对外动作经注册表**：12 项能力（出图、出视频、查询、下载、图床上传、
  写提示词、看图、判断、历史检索、知识库检索、联网搜索、配图下载）都要过同一道闸门。

### 可裁决的上下文（先看后跑）

点「开始」不直奔生成，先只跑「理解 + 找参考」，摊成一张**看得见、改得动**的卡：
来源开关（历史 / 知识库 / 联网 / 配图）、可改需求、条目逐条删、我补一句、
长期档案。**你删掉的条目本次运行不许复活**（重找一次也不会回来）。

### 知识库（上传 → 切片 → 检索）

上传 `txt / md / csv / json / pdf`，自动切片入库，和「历史作品」并列成为第二种本地来源。
检索试跑看到的，**就是助手找参考时会拿到的**（同一套 `kb.search`）。

想把网上收集的提示词放进知识库，用 [tools/import_prompt_library.py](tools/import_prompt_library.py)
先转成 Markdown（直接丢 JSON 会被切碎且检索不到，原因见脚本注释）。

### 混合检索（本地优先，不够才联网）

```
历史作品 + 知识库  ← 本地，总是先查（除非来源被关）
      ├─ 命中 ≥ 阈值 → 只用本地的，不联网（省额度）
      ├─ 命中 < 阈值 → 自动联网补齐
      └─ 你明确要求联网 → 直接联网（哪怕本地够多）
```

阈值 = `max(设置里的下限, 模型判断)` —— 模型能要求更多参考，但**压不低你设的下限**。
「为什么联网 / 为什么没联网」直接写在草稿卡上。

### 预算与审计（不烧冤枉钱）

- **预算闸门**：每项能力有单次运行上限，超了就停下问你，不静默继续。
- **重试免额度**：平台 503 / 队列满时重试的是同一个动作，**不算你的消耗**。
- **循环检测**：同一个工具用相同参数反复调用会被拦下（但**代码自己发起的轮询不算**——
  否则视频任务会在第 4 次查询被误杀）。
- **调用账目**：每次运行末尾列出「判断 1 次 · 找参考 1 次 · 出图 1 次 …」。
- **详情留痕**：历史记录里能查到平台任务 ID 与失败原因全文——事后可对账。

### 自更新（只提议，不自动改）

攒够 10 次带信号的运行后，可以让它总结规律、给出一份**提示词补丁草案**。
补丁只在你点「接受」后生效，旧版本保留、随时能回滚。能改的只有三样：
写提示词时的附加指令、画幅偏好、以及 6 个固定问题的**问法**。
**它改不了流程结构**——可行性那问不在白名单里。

## 快速开始

1. 拿到 `dist/Stillroom/` 整个目录（绿色版，不用安装）。
2. 把 `.env.example` 复制成 `.env`，放在 **`Stillroom.exe` 同级**，至少填一项：

   ```ini
   AGNES_API_KEY=sk-...
   AGNES_BASE_URL=https://apihub.agnes-ai.com/v1   # 国内版换成 api.agnes-ai.cn/v1
   ```

   也可以启动后在「设置」页里填，保存即生效、不用重启。

3. 双击 `Stillroom.exe`。

自检（不开窗口验证程序是否正常）：`Stillroom.exe --self-test`，结果写同目录 `self-test.log`。

## 无头服务（MCP）

除开窗口，Stillroom 还能以 **Agent-to-Service** 的方式被上层 Agent 调用：走
**MCP（Model Context Protocol）** 连进来，把本机当「**工作流注册中心 + 路由执行引擎**」。
Agent 只能操作 **Workflow Definition** 与 **Execution**，**不能直接碰 Kernel**。

启动（换行分隔 JSON-RPC 2.0 over stdio，**无** `Content-Length` 头）：

```bash
python -m mcp_server --identity human --db ./protocol.db
```

| 参数 | 环境变量 | 默认 |
|---|---|---|
| `--identity` | `STILLROOM_IDENTITY` | **无默认，必填**。三选一：`agent` / `human` / `system`；**未知身份直接拒绝启动**，不回落默认 |
| `--db` | `STILLROOM_PROTOCOL_DB` | `%APPDATA%\Stillroom\protocol.db`（无 `APPDATA` 时 `~/.stillroom/protocol.db`）。与 GUI 的 `history.db` **分开** |
| `--artifacts` | — | 无默认，交给 `<db 目录>/artifacts` |

> **为什么 `--identity` 不给默认值**：MCP Server 在**启动时**就确定 `creator_context`，
> 之后**不接受**来自客户端的身份输入（身份没有第二条入口）。若给个默认值，误启动的服务
> 会以某个身份**静默运行** —— 那正是安全模型的崩点。宁可不填就报错退出。

**线协议只有 3 个 MCP 方法**：`initialize` / `tools/list` / `tools/call`。
**11 个工具名（`list_skills` / `create_workflow` / `execute_workflow` / …）是
`tools/call` 的 `params.name`，不是方法名** —— 否则标准 MCP 客户端连不上。

错误分层（客户端据此分支，别匹配文案）：

- `-32602` 一律带 **`error.data.reason`** ∈ {`invalid_name` `unknown_tool`
  `invalid_arguments` `unexpected_param`} —— 全是**调用形状**问题；
- 工具自身的业务失败走返回体里的 **`isError: true`**（判据 `ok is not True`），
  **不走 `error` 字段**；`arguments` **内部**字段缺失也属这一层。

手动冒烟（发一行、收一行）：

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python -m mcp_server --identity human --db ./tmp.db
```

契约（`contracts/`：执行状态机 / MCP 工具 / 持久化）与 schema（`schemas/`）已在
**协议 v1.0** 冻结：兼容变更进次版本、**不兼容变更必须发新版本并走 deprecation 周期**。
详见 [CHANGELOG.md](CHANGELOG.md)。

## 配置项

密钥走 `.env`（exe 同级，**不出本机**），偏好走 `%APPDATA%\AgnesGenerator\settings.json`。

| 键 | 用途 |
|---|---|
| `AGNES_API_KEY` / `AGNES_BASE_URL` | 出图出视频（站点的 key 与地址必须配套） |
| `DEEPSEEK_API_KEY` / `ZHIPU_API_KEY` / `LLM_*` | 看图（视觉描述）与写提示词 |
| `TYPESAFE_API_KEY` | Jev 判断（够不够开工 / 这张行不行） |
| `GITHUB_TOKEN` / `GITHUB_REPO` 或 `SEE_API_TOKEN` | 图床（视频参考图必须是公网直链） |
| `SEARCH_PROVIDER` / `SEARCH_API_KEY` | 联网搜索（`tavily` / `bocha` / `serper` / `custom` / `off`） |

设置页可调：主题、网络模式（自动 / 仅直连 / 仅系统代理）、数据目录、
草稿或自动模式、四个来源开关、本地参考阈值、联网 provider、生成默认值。

## 数据放在哪

默认 `F:\AgnesGeneratorData`（设置页可改，也能一键打开）：

| 内容 | 位置 |
|---|---|
| 历史记录、运行时上下文、长期档案 | `history.db`（SQLite） |
| 知识库切片 | 同一个 `history.db` 的 `kb_chunks` 表 |
| 知识库原文件 | `knowledge/`（命名 `<记录id>__<原文件名>`） |
| 生成的图片 / 视频缓存 | `media/`、`thumbs/` |
| 联网配图 | `web_refs/` |
| 意外异常现场 | `logs/error.log` |

## 已知限制

诚实列出来，免得你按错误预期使用：

- **个人项目**：6 天、45 笔提交，成熟度远不如无限画布那类项目。
- **无插件系统**：12 项能力是闭集。闭集才好保证「预算 / 审计 / 白名单」全部生效。
- **单助手、单运行**：没有多 Agent 协同。
- **视频不能自动评估**：视觉模型只吃图片，抽帧要额外依赖。现在如实说
  「视频暂不能自动评估，请你自己看一眼」，**不假装它会自动评判**。
- **PDF 不做 OCR**：扫描件提不出文字时直接告诉你，不猜内容。
- **联网配图只是参考**：默认关、带免责声明，**不会**当最终产物发布。
- **短剧链路未做**：多镜头拼接、配音、字幕、转场都还没有。

## 文档

本仓库只放可运行的代码。**设计文档、实施计划与调试记录不在开源仓库中**
（它们涉及较多本地环境细节，且处于持续修改状态）。

仓库内唯一保留的文档是 [CHANGELOG.md](CHANGELOG.md)：版本记录与更名说明。

## 许可

[PolyForm Noncommercial License 1.0.0](LICENSE)

**可以**：阅读、学习、研究、自己玩、改着玩、非商业地分享（要带上这份许可）。

**不可以**：商业用途——包括把它或它的修改版拿去卖、用在收费产品或服务里。

> Required Notice: Copyright zzq (https://github.com/q93304989-bit)

## 开发

```powershell
.venv\Scripts\python.exe -m pytest -q -o addopts=""   # 全量测试
.venv\Scripts\python.exe tools\prove_mcp_server.py    # 变异反证（27 场景；本机需按场景名分批跑）
.venv\Scripts\python.exe tools\build.py --clean       # 打包（产物 dist/Stillroom/）
```

> 本机 `PySide6` DLL 加载失败时，纯 GUI 模块采集即失败、图缩放类用例报「图片无法解码」。
> **排除这些文件后的无头子集仍应全绿**；12 项环境失败的清单与 A10 的复验要求见
> [CHANGELOG.md](CHANGELOG.md)。

架构分层（依赖只能向下，不能向上或成环）：

```
（GUI 侧，依赖 PySide6）
app.config       ← 路径 / 密钥 / 偏好，三个唯一入口
app.net          ← 出网与错误分类
app.clients      ← 图片 / 视频 / 图床 / LLM / 视觉 / 判断客户端
app.capabilities ← 能力注册表（工具描述 + 元数据 + 闸门）
app.services     ← 编排（生成 / 检索 / 知识库 / 历史 / 上下文）
app.agent        ← 六步流水线（阶段 + 判断 + 精修）
app.ui           ← 界面（桥 + QML）
app.adapters     ← GUI 接协议层的最小客户端（数据只走它，界面不碰协议）

（无头侧，刻意不依赖 PySide6）
mcp_server/      ← 线协议（3 个 MCP 方法）+ 11 个工具（入口层）
runtime/         ← repository / registry / artifacts / stub_kernel / router
validator/       ← 执行状态机 + 四层校验 + 错误码
schemas/         ← JSON Schema
```

依赖方向：`mcp_server/ → runtime/ → validator/ → schemas/`，且无头层**不得** import
PySide6 或 `app.*`（由 `tests/test_headless_boundary.py` 强制）。
