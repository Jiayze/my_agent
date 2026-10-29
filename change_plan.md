# my_agent 综合修改与演进计划（最终定稿版）

> 本文档由 `change_plan_for_review.md` 吸收外部评审与多源源码核验结论后合并定稿。
> 本文档为 `my_agent` 从 M4（无状态 Agent + 短期记忆）至后续里程碑演进的唯一权威修改基准。
> 所有参考项目的源码引证严格绑定本地 `_ext/` 树四个已验证的 Git HEAD 标本：
> - `deepseek-harness`: `c291e796`
> - `hermes-agent`: `6e07eb48`
> - `codex`: `b348fc26`
> - `autogpt`: `98381ab2`

---

## 0. 核心架构不变量（最高宪法，全阶段不可降级）

1. **fail-closed**：未显式允许的一律拒绝；解析失败、网络无法证明、审批通道不可用一律拒绝，绝不 fail open。
2. **内建 `forbidden` 不可提权**：内建禁止项（如删除根目录、写系统目录、Git 历史改写）位于任何审批和 grant 之上，任何授权均无法放宽。
3. **通道严格隔离**：用户通道的 TOTP 免输码窗口（grace window）只属于用户显式交互，绝不使任何 Agent 请求免批。
4. **单次精确绑定授权（Per-operation Grant）**：目录外授权只绑定单次 `request_id` + 规范化精确路径 + 内容哈希 + 操作类型，用后即废，不修改常驻 session mode。
5. **控制平面与数据平面分离**：工作区文件（`AGENTS.md` 等）只能表达人类说明与建议，只能净收紧、绝不能净放宽安全策略；策略权威永远在代码层 `PolicyResolver`。
6. **Shell 真实边界属于 OS**：进程内 Python 校验管不住子进程；在 Windows 后端能从物理上证明断网和写收缩之前，Agent Shell 保持关闭。
7. **部分执行状态诚实**：未知结果不能自动重试，状态机必须诚实记录未知/中断。

---

## 1. 架构决策终审结果（原 §4 决策项已定稿）

| 决策项 | 最终裁决 | 核心依据与落地形态 |
|---|---|---|
| **C1: Shell 后端选择** | **调用方选策略，runner 自动探针匹配，不把后端选型暴露给用户** | 借鉴 DSH `sandbox-local` 的 runner chain。调用方表达安全需求（断网、目录限制），由系统探针挑后端；若后端无法证明落实则 fail-closed |
| **C2: Windows 下 Agent Shell 开放时机** | **延后开放（选 A）** | Windows 受限 Token 无法表达网络隔离（DSH 自认“网络不受限”；Codex 网络过滤需独立身份 WFP）。在独立身份后端证明无出站网络前，Agent Shell 保持禁用 |
| **C3: 升权阶梯落地形态** | **文件侧走单次 grant，升权阶梯仅处理网络与 sandbox mode** | 杜绝文件授权的双重真相。`danger-full-access` 仅作为初始词汇待复审，任何情况下仍受内建 `forbidden` 约束 |
| **C4: 临时会话沙盒化与自身代码锁定** | **未配工作区自动进沙盒目录，自身源码永久只读硬锁定** | 废除回退到 `PROJECT_ROOT` 的自修改漏洞。临时会话落入 `data/workspaces/<id>/`；`src/`、`.env` 永久加入底层 `protected_denied` |
| **C5: 命令行审批呈现与侧车模型** | **三层审批呈现：客观静态事实 + 独立侧车审查解读 + 主 Agent 理由** | V0.1~V0.4 手动复制为过渡阶段；V0.5.1 原地挂起弹窗。由独立侧车模型（无历史污染）解读副作用，最终由人类授权 |
| **C6: 四层解耦上下文治理体系** | **入口硬截断 + 发送投影清洗 + 基于逻辑轮次的软阈值压缩 + 单次溢出恢复** | 摒弃 DSH 虚高 99% 回放；在 `ToolNode` 边界硬截断（防 UTF-8 碎裂，保留结构化 JSON）；解耦 `CanonicalState` 与 `ProviderProjection`（按模型动态清洗）；逻辑轮次压缩（按 16k-32k 软阈值，防 128k 晚期注意力涣散） |
| **C7: Jev 决策模型定位与候选场景** | **定位为 System-1 辅助决策层，明确风险前提，列入三大候选场景** | ①**技术风险先决条件**：明确 Jev 虽然物理免疫提示词劫持（无文本生成循环），但仍存在对抗样本逃逸（漏报）、局域语境误报（误杀）与外部服务延迟抖动风险，**绝不可作为单点唯一安全边界，且不可用时必须能平滑降级**；②**三大候选场景**：场景 2（非信任外部输入的杀毒扫描网：10ms 预检提示词注入并加信封保护）、场景 4（任务完成度防忽悠检验：对照测试结果与最终回复，防止过早谎报完成）、场景 5（用户需求歧义度拦截：输入端毫秒级判定模糊意图并提示 Clarify）；③**分期规划**：V0.2 预留上下文剪枝接口，V0.6 引入模型路由与记忆守门，V0.7+ 驱动浏览器操作 |

---

## 2. 代码修复与加固计划（落实至源码）

| # | 目标文件 | 级别 | 核心问题 | 最终定稿方案 |
|---|---|---|---|---|
| **A1** | `src/my_agent/conversation.py` | **P0** | `_validate_result_prefix` 先查总长再比 ID，导致“同时缩短且改写”误报为丢消息（唯一红测试） | 改用**以原前缀（`run_messages`）为主体的单次线性遍历**：`result[i].id != expected.id` 判篡改；`i >= len(result)` 判丢弃；循环后判是否产生新回复。严格先查篡改、后查丢失 |
| **A2** | `src/my_agent/conversation.py` | **P1** | `expected_id is not None` 豁免漏洞：中间 AI/Tool 消息无 ID，被跳过校验 | 在 `_compact()` 出口向 `Agent.invoke` 提交前，对所有消息全量补齐 `uuid4().hex`，彻底移除 `is not None` 豁免，消灭免检通道 |
| **A8** | `src/my_agent/conversation.py` | **P1** | 内存对象共享：实测 `result.messages[i] is run_messages[i]` 为 `True`，下游改动会直接污染当前历史 | **边界结构深克隆（防）**：借鉴 Hermes `_clone_message_for_send`（`conversation_loop.py:914-921`），在交入 `invoke` 前做轻量容器深克隆（递归复制 dict/list，不可变叶子共享），物理切断别名 |
| **A3** | `src/my_agent/agent.py` | **P2** | 缺少元数据的工具被静默当成只读放行，违反 fail-closed | 显式校验：缺失 `effect` 元数据直接报错拒绝 |
| **A4** | `src/my_agent/prompts.py` | **P2** | 提示词与计划冻结规则漂移 | 统一对齐为已实装的安全卫生规则 |
| **A5** | `src/my_agent/agent.py` | **P3** | `final_messages[:-1]` 空回复切片死代码 | 消除死代码，将空回复处理合并至 `terminal_error` 统一出口 |
| **A6** | `src/my_agent/agent.py` | **P3** | `OpenAIError` 脆弱 fallback 导入 | 依赖已明确，直接标准导入 |
| **A7** | `tests/test_agent.py` | **P3** | 未实测断言 LangGraph 步数上限 | 补齐步数边界测试断言（3N+5 步安全余量） |

---

## 3. 会话完整性与内存隔离演进（Scheme C，分阶段落地）

### 阶段一：M4 当下闭环（单进程轻量充分防护）
1. **物理隔离（防）**：`_clone_message_for_invoke` 切断引用别名；
2. **逻辑核验（查）**：以旧前缀为主体线性遍历，拦截篡改与丢失；
3. **全量补号**：确保所有进入状态机的消息具备唯一 ID；
4. **措辞收缩**：将 `agent_plan.md` 的不变量表述从“不得被篡改”收紧为“保证输入前缀不被丢弃与重排（Prefix retention & order invariant）”。

### 阶段二：V0.6 持久化闭环（跨进程/存储端到端防篡改）
1. 在 SQLite 事件存储（`ConversationStore`）序列化边界引入内容摘要；
2. 规范化借鉴 Hermes `_content_cache_key`：`sort_keys=True`、`ensure_ascii=False`、`separators=(",", ":")`、`\x00` 防边界伪造；
3. 签名封签与持久记录同行，保障跨会话恢复时的端到端自校验。

---

## 4. `agent_plan.md` 文档终审修改清单（已定稿待同步）

| # | 计划位置 | 最终定稿内容 | 理由 |
|---|---|---|---|
| **B1** | §0.1 | 钉死本地 `_ext/` 树四个 HEAD（DSH `c291e796`、Hermes `6e07eb48`、Codex `b348fc26`、AutoGPT `98381ab2`） | 消除不可复核的历史歧义 |
| **B2** | §9.5 | 修正措辞：承认未知工具由 LangGraph `ToolNode` 内建回填，`wrap_tool_call` 专职处理业务异常 | 对齐现有测试断言与框架机制 |
| **B3** | V0.3 | 补齐 `results_digest` 结果侧规范化规则 | 排除时间戳与随机值，使 stall 检测切实生效 |
| **B4** | 决策 12/18 | 确立 `/remote` 威胁模型：用户显式控制通道，非 Agent 能力，Agent 无法触发，不受 Agent mode/effect 约束 | 消除安全不对称性 |
| **B5** | M4 验收 | 改为合成 `BaseTool` + `effect="host_command"` 验收前向拒绝不变量 | 转化为当下可测的契约 |
| **B6** | V0.4-V0.6 | 规格瘦身：参数移入附录作为初始值，硬锁定 8 条核心不变量 | 避免无用户阶段的过度预承诺 |
| **B7** | V0.6 | 保留 fencing token：SQLite CAS 只负责租约获取，每个副作用前仍须校验 fencing token | 彻底防范过期 owner 延迟写入 |
| **B8** | M4 | 彻底修好 A1，使测试全绿 | 消灭常红污染 |
| **B9** | 全文 | 审批状态词汇全局统一为 `allowed-once` | 消除动作名与状态名歧义 |
| **B10** | V0.5.1 | 澄清升权 schema：请求字段在 schema 中钉死目标词汇，但“严格更宽”等决议只能在运行时由 resolver 判定 | 消除 schema 自相矛盾 |
| **B11** | 决策 20 | 形式化 `mode × effect`：废除无意义的大小比较，改为能力集覆盖判定 | 消除类型系统无法比较的缺陷 |
| **B12** | 决策 21 | 闭合 waterfall 审计：明确多决策者场景下以一次完整请求成对记录 asked/decided，内部委派仅记诊断日志 | 保持审计日志数量不变量 |
