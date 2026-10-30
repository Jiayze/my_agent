# my_agent

个人本地 Agent 框架（Windows / 单用户 / Python 3.10+）。
当前版本推进到 **V0.5（可用的 CLI coding agent）**：V0.1 离线核心与 CLI、
V0.2 上下文压缩、V0.3 只读访问层与防卡死账本、V0.4 工作目录内写入、
V0.5 命令执行（每条命令逐次人工批准）。

## 当前能力

- 从 `.env` 或进程环境加载并校验 `AgentConfig`，启动摘要不包含 API key。
- 使用 `langchain-openai` 创建 OpenAI 兼容聊天模型；可选启用 DeepSeek 思考模式
  并在携带 `tools` 的多轮请求中完整回传 `reasoning_content`。
- 无状态 `Agent` 基于 LangGraph 完成“模型 → 工具 → 模型”的闭环，工具轮数有硬上限。
- **V0.2 上下文压缩**：`Conversation` 按 token 压力（`usage_metadata` 优先、
  保守字符估算兜底）依次执行模型无关剪枝 → 增量摘要 → 硬截断回退；
  摘要模型独立配置、缺省回落主模型；压缩以完整轮次为单位。
- **V0.3 进度账本防卡死**：每轮工具调用的规范化签名（工具名 + 参数摘要 +
  结果摘要）写入 `progress_log`，连续重复达到 `MAX_STALL_ROUNDS` 返回
  `AgentResult(error_code="agent_stall")`；被拒或失败的调用同样计数。
- **V0.3 只读访问层**：`list_directory`、`read_text_file`、`search_text`
  （固定 ripgrep）、`git_status`、`git_diff`（固定 git 子命令），全部
  `effect=read_only`，共享一套路径边界与敏感文件策略；可选
  `search_knowledge_base`（`KNOWLEDGE_BASE_ROOT`，与工作目录强制分离）。
- **V0.4 工作目录内写入**（仅 coding profile，`build_conversation(profile="coding")`
  显式装配）：`write_text_file`、`replace_text`；每次写入先持久化 intent
  （prepared → snapshot_ready → executing → done，SQLite + fsync）、生成快照、
  持有按规范化路径划分的进程内写锁与 fencing token；覆盖前要求先读过且版本
  未变（`FS_STALE_VERSION`）；支持按快照确定性回滚；启动恢复丢弃未完成
  intent、清理孤儿快照、把遗留 `executing` 标记为结果未知并告警，绝不自动重试。
- **V0.5 命令执行**（coding profile，`--profile coding` 显式启动）：
  `run_command` 工具经 `cmd.exe /d /s /c` 执行命令；每条命令先在终端
  逐次人工批准（[y/N]），删除文件、`git push/reset/revert/rebase/clean`、
  修改系统配置等命令被策略**永久拒绝**（位于审批之上）；执行走
  intent → 审批 → executing → 审计的完整记账，超时强制终止整棵进程树，
  输出有界捕获并脱敏；结果状态区分 succeeded/failed/timed_out/denied/
  unavailable/interrupted。
- 审批按**决策者链**建模（决策 21）：当前只有人类 CLI 决策者；`never`
  模式确定性拒绝；决策者异常或链尾无决定一律 fail closed。
  **诚实声明：当前部署没有 OS 级沙盒**——命令以当前用户完整权限运行，
  工作目录只是初始目录、不是安全边界；补偿控制是逐命令人工批准 +
  禁止清单 + 有界输出 + 审计。受限 token / AppContainer 等 OS 后端 POC
  仍是计划内 Shell 全面启用的前置条件。
- 同一模型响应只要包含写工具或命令执行，整批按模型返回顺序串行执行；
  纯只读批次并行。
- 目录外写入一律拒绝并返回惰性建议文本；`.env`、密钥、`.git`、`.venv`、
  规则文件（`AGENTS.md` 等）永不可写；grant（目录外一次性授权）只交付
  数据模型，执行通道最早随 V0.5.1 开放。
- CLI 支持连续对话、空输入忽略、`/exit`、`/quit`、EOF 与 Ctrl+C 退出。
- 标准库日志写入 stderr，支持按配置设置级别，并在输出前脱敏 API key。

当前**不包含** Shell、目录外写入执行、TOTP/命令通道、QQ、RAG、多 Agent 或
长期记忆；这些属于后续路线（V0.5+）。

截至 2026-10-31，本阶段离线实现全部通过：**377 passed、4 skipped**，
Ruff lint/format 通过。真实 DeepSeek API 的工具闭环及 reasoning 回传仍为
**LIVE_UNVERIFIED**；离线 `MockTransport` 契约测试不代表真实服务已经验收。

## 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 配置

复制 `.env.example` 为 `.env`，填入真实值。不要提交 `.env`。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `CHAT_MODEL_API_KEY` | 是 | 模型服务密钥；不会出现在启动摘要、日志或用户输出中 |
| `CHAT_MODEL_ID` | 否 | 模型 ID，默认 `deepseek-v4-flash` |
| `CHAT_MODEL_BASE_URL` | 否 | OpenAI 兼容地址，默认 `https://api.deepseek.com` |
| `CHAT_MODEL_REQUEST_TIMEOUT_SECONDS` | 否 | 请求超时秒数，默认 `60` |
| `CHAT_MODEL_TEMPERATURE` | 否 | 温度，范围 0–2，默认 `0` |
| `MAX_TOOL_ROUNDS` | 否 | 单轮工具调用上限，范围 1–10，默认 `4` |
| `CHAT_MODEL_THINKING` | 否 | `enabled` / `disabled`，默认 `disabled`；仅对 DeepSeek 思考模式显式 opt-in |
| `CHAT_MODEL_REASONING_EFFORT` | 否 | 思考强度 `low` / `high` / `max`，默认 `high`；仅在思考模式启用时发送 |
| `SUMMARIZER_MODEL_ID` | 否 | V0.2 摘要模型；留空回落主模型 |
| `SUMMARIZER_MODEL_BASE_URL` | 否 | 摘要模型地址；留空回落主模型 |
| `SUMMARIZER_MODEL_API_KEY` | 否 | 摘要模型密钥；留空回落主模型 |
| `CONTEXT_TARGET_TOKENS` | 否 | 压缩软阈值，默认 `32000` |
| `CONTEXT_HARD_LIMIT_TOKENS` | 否 | 硬上限（≥ 软阈值），默认 `48000`；超过只允许硬截断 |
| `KEEP_RECENT_TURNS` | 否 | 压缩后保留的原始完整轮数，默认 `3` |
| `MAX_STALL_ROUNDS` | 否 | 防卡死连续重复轮数阈值，范围 2–5，默认 `3` |
| `KNOWLEDGE_BASE_ROOT` | 否 | 知识库目录（只读，必须与工作目录分离）；未配置时不注册检索工具 |
| `WORKSPACE_ROOT` | 否 | 工作目录；缺省为项目根目录 |
| `WRITE_MAX_BYTES` | 否 | V0.4 单次写入内容字节上限，默认 1 MiB |
| `SHELL_APPROVAL` | 否 | V0.5 命令审批两态：`ask`（逐条询问）/ `never`（确定性拒绝），默认 `ask` |
| `SHELL_TIMEOUT_SECONDS` | 否 | 命令默认超时（也是单次请求的上限），范围 5–600，默认 `120` |
| `LOG_LEVEL` | 否 | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`，默认 `INFO` |

## DeepSeek 思考模式

默认关闭，保持原有 OpenAI 兼容路径。只有显式设置 `CHAT_MODEL_THINKING=enabled`
才会启用 DeepSeek 专用适配器：

- 请求通过 `extra_body={"thinking": {"type": "enabled"}}` 打开思考，并用顶层
  `reasoning_effort` 指定 `low` / `high` / `max`。
- 思考模式不支持 `temperature`，启用时不会发送该参数（配置值保留但不生效）。
- `reasoning_content` 是服务端返回的内部推理，只保存在消息对象中用于协议回传；
  不会打印到终端、不会写入日志，也不会被改写成最终回答。
- 只要请求携带 `tools`，历史中每条 assistant 消息的 `reasoning_content` 都会
  原样回传，包括没有 tool call 的回合；缺失该字段的回合不会伪造内容。
- 非 DeepSeek 的自定义 base URL 不会被自动套用；需要端点兼容该协议时再显式 opt-in。

官方 `langchain-deepseek` 目前只解码、不回传 `reasoning_content`，因此本项目在
`src/my_agent/deepseek_chat.py` 提供局部兼容层，只覆写解码与序列化，不改动
`langchain-openai` 本身。

## 启动

在项目根目录任选一种：

```powershell
.\.venv\Scripts\python.exe -m my_agent
```

```powershell
.\.venv\Scripts\my-agent.exe
```

退出方式：

- 输入 `/exit` 或 `/quit`（允许尾部空格）。
- 输入阶段按 `Ctrl+C` 或 `Ctrl+Z`（EOF）。
- 其他以 `/` 开头的文本按普通消息交给 Agent。

启动 coding profile（读写文件 + 逐条批准的命令执行）：

```powershell
.\.venv\Scripts\python.exe -m my_agent --profile coding
```

CLI 默认使用 read-only profile；写入与命令能力只在显式的 coding profile 装配，
见 `my_agent.main.build_conversation(profile="coding")`。命令执行过程中，
每条命令都会先在终端打印命令全文并等待 `批准执行? [y/N]`。

## 演示工具

启动后输入类似：

```text
请用 add_numbers 计算 2 加 3。
```

模型应调用 `add_numbers` 并返回 `5`。工具只在模型主动选择时执行。

## 测试与静态检查

离线测试不访问网络，`live` 测试默认跳过：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

显式运行真实模型 smoke test（会消耗额度）：

```powershell
$env:RUN_LIVE_TESTS = "1"
.\.venv\Scripts\python.exe -m pytest -q tests/test_live.py
```

思考模式工具闭环 live 测试需要同时显式开启开关；它最多调用模型 6 次，失败即停止，
只输出工具名/参数/结果、调用次数和 reasoning 是否存在及长度，不记录推理正文：

```powershell
$env:RUN_LIVE_TESTS = "1"
$env:CHAT_MODEL_THINKING = "enabled"
.\.venv\Scripts\python.exe -m pytest -q tests/test_live.py -k thinking -s
```

## 日志与错误边界

- 用户对话输出写入 stdout；日志写入 stderr，默认级别由 `LOG_LEVEL` 控制。
- 日志记录模型与工具的开始、结束、耗时、错误阶段、异常类型和 `run_id` / `conversation_id`。
- 日志不记录完整系统提示词、完整用户消息或 API key；输出前还会经过一次密钥脱敏过滤。
- 配置错误和模型初始化失败以退出码 `1` 结束，并向 stderr 输出固定安全提示。
- 单轮可预期的模型/工具失败返回安全 `AgentResult`，CLI 显示友好错误后继续下一轮。
- 单轮取消（执行阶段 Ctrl+C）只取消本轮；未预期的编程/框架异常会记录脱敏 traceback
  后继续向上传播，不会被静默吞掉。
- V0.4 写入失败的 intent 按结果未知处理并告警，绝不自动重试非幂等副作用。

## 目录

```text
src/my_agent/
├── main.py              # CLI 组合根、profile 装配与交互循环
├── __main__.py          # python -m my_agent
├── config.py            # 配置加载与校验（V0.2/V0.3/V0.4 字段）
├── llm.py               # 聊天模型工厂
├── deepseek_chat.py     # DeepSeek 思考模式 reasoning_content 兼容层
├── agent.py             # 无状态 LangGraph Agent + 进度账本防卡死 + 副作用批次串行
├── bounded_process.py   # 共享有界子进程执行（超时终止进程树/输出上限）
├── exec_policy.py       # V0.5 命令分类（allow/prompt/forbidden）
├── approval.py          # V0.5 审批决策者链（人类 CLI 决策者）
├── command_exec.py      # V0.5 CommandExecutionService（intent+审批+审计）
├── conversation.py      # 短期记忆与消息协议校验
├── compaction.py        # V0.2 上下文压缩（剪枝 → 摘要 → 硬截断）
├── logging_setup.py     # 日志配置与密钥脱敏
├── models.py            # 共享领域类型
├── prompts.py           # 系统提示词
├── side_effects/        # V0.4 副作用基础设施
│   ├── state_dir.py     #   用户独占 DACL 的应用状态目录
│   ├── intent_store.py  #   SQLite intent 状态机 + grant 数据模型
│   ├── snapshots.py     #   快照存储与确定性回滚
│   ├── path_locks.py    #   按规范化路径的写锁与 fencing token
│   └── service.py       #   SideEffectService（intent→快照→executing→done）
└── tools/               # 工具注册表与内置工具
    ├── path_policy.py   #   共享路径边界与敏感策略
    ├── repo_read.py     #   五个只读仓库工具
    ├── knowledge.py     #   知识库检索（V0.3）
    └── repo_write.py    #   工作目录内写入工具（V0.4，coding profile）
```

## 后续路线

V0.5 用户命令通道与 TOTP（`/command`）→ V0.5.1 OS 沙盒 POC、一次性目录外
grant 与 pending/resume → V0.5.5 SSH 备用通道 → V0.6 QQ 与 ConversationStore →
V0.7 桌面 → V0.8 长期记忆。详见 `agent_plan.md`。
