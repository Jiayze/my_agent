# my_agent

个人本地 Agent 框架（Windows / 单用户 / Python 3.10+）。
当前版本完成 M0–M4 的离线核心，并提供 M6 最小 CLI 与 M7 日志、Ruff、文档基线。

## 当前能力

- 从 `.env` 或进程环境加载并校验 `AgentConfig`，启动摘要不包含 API key。
- 使用 `langchain-openai` 创建 OpenAI 兼容聊天模型。
- 可选启用 DeepSeek 思考模式，并在携带 `tools` 的多轮请求中完整回传 `reasoning_content`。
- 无状态 `Agent` 基于 LangGraph 完成“模型 → 工具 → 模型”的闭环，工具轮数有硬上限。
- `Conversation` 按 `conversation_id` 持有短期记忆，按完整轮次截断历史。
- 内置只读演示工具 `add_numbers`。
- CLI 支持连续对话、空输入忽略、`/exit`、`/quit`、EOF 与 Ctrl+C 退出。
- 标准库日志写入 stderr，支持按配置设置级别，并在输出前脱敏 API key。

当前**不包含** Shell、文件写入、QQ、RAG、多 Agent 或长期记忆；这些属于后续路线。

截至 2026-10-29，本阶段离线实现与独立复审已通过，完整测试为 **193 passed、3 skipped**。
真实 DeepSeek API 的工具闭环及下一轮 reasoning 回传仍为 **LIVE_UNVERIFIED**；
离线 `MockTransport` 契约测试不代表真实服务已经验收。

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
| `WORKSPACE_ROOT` | 否 | 工作目录；缺省为项目根目录 |
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
- 单轮取消（执行阶段 Ctrl+C）只取消本轮；未预期的编程/框架异常会记录脱敏 traceback 后继续向上传播，不会被静默吞掉。

## 目录

```text
src/my_agent/
├── main.py            # CLI 组合根与交互循环
├── __main__.py        # python -m my_agent
├── config.py          # 配置加载与校验
├── llm.py             # 聊天模型工厂
├── deepseek_chat.py   # DeepSeek 思考模式 reasoning_content 兼容层
├── agent.py           # 无状态 LangGraph Agent
├── conversation.py    # 短期记忆与按轮次截断
├── logging_setup.py   # 日志配置与密钥脱敏
├── models.py          # 共享领域类型
├── prompts.py         # 系统提示词
└── tools/             # 工具注册表与内置工具
```

## 后续路线

V0.2 上下文压缩 → V0.3 只读文件与知识库 → V0.4 工作目录内写入 → V0.5 用户命令通道与 TOTP → V0.5.1 Shell 沙盒与审批 → V0.6 持久会话 → 之后才是 QQ、桌面与长期记忆。
