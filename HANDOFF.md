# Alice Agent v0.9.1 交接文档

最后核验日期：2026-10-19（主动消息链路与每日概率；历史实例记录保留原日期）
项目路径：`C:\Users\Administrator\.astrbot\data\plugins\astrbot_plugin_alice_agent`  
当前版本：`0.9.1`；源码与安装包以GitHub main及Releases实际记录为准。
AstrBot 兼容声明：`>=4.25.5,<5`

本文档面向接手该项目的新对话或新开发者。它描述当前工作区真实存在的实现，不代表最初需求文档中的所有设想都已经完成。

## v0.9.1 主动消息更新

默认每日事件开关仍为false，打开主动开关不保证有候选。普通事件低社交需求时评分约0.3495，旧门槛0.45会长期拦截；新默认0.35，既有服务器配置需手动调整。核对本机配置时 daily_events_enabled/proactive_enabled/agent_loop_enabled 均为true，但这不是服务器证据。

新增每天一次的分享概率：规则看心情、精力、压力与事件，默认额外模型判断意愿再各占一半混合；每会话每天一次抽签，正反结果均写入新增SQLite表。关闭模型可只用规则。没有抽中时不规划，不按Tick反复抽签；次日首次满足条件重新判断。概率表示允许进一步规划，最终发送仍受其它门控约束。

后台INFO显示实际开关、本地时间/时区、日常门槛、滚动24小时预算及具体拦截原因；同原因每会话每小时最多一次。保留15分钟活跃保护、7天活跃范围、未回复保护、冷却与预算。补齐模型期间新消息取消及发送前时段/日期/冷却复核。

本机当前QQ官方适配器有私聊主动发送实现，Context旧的“不支持”注释不能作为平台结论。框架布尔返回不等于平台送达回执。未取得服务器日志和配置，未做真实模型/QQ验收。

配置、概率公式、成本、排查和剩余限制以 [docs/PROACTIVE.md](docs/PROACTIVE.md) 与 [SERVER_UPDATE.md](SERVER_UPDATE.md) 为准。包含此前本地v0.9.0的228事实/28对白。新增12项，全量269项本地回归；模型/发送为fake，重启验证使用临时真实SQLite。此前257项等记录为历史基线。

## 1. 接手前必须知道的事实

1. 当前 Memory 后端是纯本地 SQLite。
2. 当前项目中没有 TencentDB Adapter、MemoryCore HTTP、L0/L1/L2/L3 远端接入代码。
3. 不要因为旧对话曾经提到 TencentDB 就假设它仍然存在；如果未来重新接入，必须通过 `MemoryPort` 新增 Adapter。
4. Jev Decision Layer 已实现；schema 默认 `jev_enabled=false`、`jev_mode=shadow`。最近核对的本机实例显式启用 Jev 且处于 `shadow`；这是实例配置，不是插件默认值。v0.8.2 中 `shadow` 的分类/行为决策仍用原路径，但用户要求的回复情绪 Jev／本地 VAD 混合会执行，不能再称 shadow 结果完全不生效。
5. Jev 首次启用应使用 `shadow`，不要直接切换到 `primary`。
6. 本次本地回归没有调用真实模型或平台发送；当前实例的 Jev shadow 配置不构成准确率、成本或真实送达验收。
7. Response Generator 优先使用 AstrBot 当前会话的 LLM Provider；2026-10-11 修复后，失败时按当前会话配置顺序尝试备用模型。Jev 不生成回复文本。
8. `alice_agent/` Core 不依赖 AstrBot、httpx、TypeSafe SDK 或 TypeSafe endpoint。
9. Reactive 回复支持可选 Genie TTS 与表情包选择（默认 `media_enabled=false`）；QQ 官方平台语音独立发送，主动消息仍为纯文本，文本均可逐句发送。
10. v0.9.0（2026-10-13）新增19项，全量257项、源码编译和配置schema解析通过；28条对白逐字及747处源证据逐行核对通过。v0.8.4的238项与更早数量为历史基线。
11. 本地语义 VAD 默认启用；Jev 是否运行仍由现有 `jev_enabled` 与凭据控制，未启用时不调用远端。
12. 逐轮诊断 JSON 默认关闭；启用会保存敏感私聊文本及模型/状态诊断。
13. 此前本机已完成 Genie TTS 与本地 WAV→QQ Record→Silk 贯通验证，但尚未做 QQ 平台实际送达验收。
14. 2026-10-11 已在项目目录初始化独立 Git 仓库（`main`），远端为 `https://github.com/3039149572/Alice-agent.git`。仅跟踪源码、提示词、配置字段定义、文档和测试；本地音频、表情、缓存、压缩包和运行数据不提交。服务器安装与更新见 `SERVER_UPDATE.md`。
15. `daily_events_enabled` 的 schema 默认仍为关闭；2026-09-29 只读核对的本机配置显式为 `true`，属于实例值。每日生活分享依赖每日事件开启，不是强制每日发送。
16. 2026-09-29 的历史实例记录为 `media_enabled=true`、`media_emoji_enabled=true`、`test_log_enabled=true`、`daily_events_enabled=true`、Jev `shadow`、`jev_review_threshold=0.60`；这些不是插件默认值，也未在本次核对为当前值。Jev 实际采用下限仍固定 0.80。代码修改没有重载插件或改写运行配置。
17. v0.8.1 的回复风格提示由本地规则构造，不增加 LLM/Jev 调用；表情语义对齐是有限的确定性筛选，不读取图片像素、不保证中文语义准确，也未做真实 QQ 平台送达验收。
18. v0.8.4 补齐按实际闲置时间进入/退出休息的确定性规则，默认保存无文本的 Jev 汇总指标；TTS 原文/译文日志改为默认关闭，诊断保留期限为可选配置。详细变更见 `CHANGELOG.md`。
19. v0.9.0 默认人格改为整合骑士爱丽丝，没有童年滋贝鲁库的亲历记忆；可以了解他人转述，不把资料、名字熟悉感或情绪当成记忆恢复。小说知识与用户真实聊天记忆分开，宽泛tag不触发召回。静态资料和接入说明见 `docs/NOVEL_CONTEXT.md`。

## 2. 当前总体定位

Alice Agent 已从单纯 Reactive Chatbot 演进为：

```text
AstrBot Adapter
    ↓
Framework-independent Alice Core
    ├── Reactive message pipeline
    ├── Persistent relationship/memory/autonomy data
    ├── Event Bus + Agent Loop
    ├── Drive + proactive policy
    ├── Optional Jev bounded-decision layer
    └── Optional reactive TTS / emoji media output
```

职责划分：

```text
AstrBot
  平台事件、Provider、消息发送、插件生命周期

Alice Core
  Perception、State、Relationship、Memory、Planner、Drive、Agent Loop

Deterministic Code
  数值状态、评分、阈值、Cooldown、Budget、Policy

Jev
  bounded classification / routing / scoring / gating

LLM
  复杂推理与自然语言生成
```

最重要的设计原则仍是：

```text
Deterministic rules
        ↓
Jev bounded judgment
        ↓
Deterministic policy
        ↓
LLM generation
```

## 3. 已实现功能明细

| 模块 | 当前状态 | 实际能力 |
|---|---|---|
| AstrBot Adapter | 已完成 | 私聊 Handler、消息转换、Provider 复用、主动发送、生命周期管理 |
| Agent Core | 已完成 | 与 AstrBot 解耦的异步 Pipeline |
| AgentEvent | 已完成 | event/trace/source/session/user/payload/metadata 统一模型 |
| Perception | 已完成 | 本地语义 VAD 情绪基线、LLM fallback、可选 Jev bounded perception/语义对比 |
| Short-term State | 已完成 | session 隔离、Reducer 更新、时间衰减 |
| Daily Events & Mood | 已完成可选基础 | 本地小事件、可选 LLM/Jev、有界情绪影响、SQLite 跨日恢复；默认关闭 |
| Relationship | 已完成 | user 隔离、SQLite 持久化、保守增量更新 |
| Local Memory | 已完成 | SQLite 内容存储、原始轮次记录、user/session scope |
| Novel Character Knowledge | 已完成本地基础 | 228张只读剧情卡与28条精选对白；具体主题检索、阶段和知情边界、有限上下文；无向量或新增模型调用 |
| Memory Metadata | 已完成 | importance/emotion/relationship/access/pinned 等 SQLite metadata |
| Memory Reranker | 已完成 | relevance gate、代码评分、Top N、DEBUG explainability |
| SharedExperience | 部分完成 | 模型和持久化入口存在；没有自动提取 |
| Planner | 已完成 | LLM Planner、结构化 ActionPlan、可选 Jev bounded planner |
| Response Generator | 已完成 | Persona + State + Plan + selected Memory → LLM 文本；v0.8.1 增加本地生成的详略/纠正/重复开场提示，不增加模型调用 |
| Event Bus | 已完成 | 统一事件与 recent event ID 去重 |
| Agent Loop | 已完成 | 可启动/停止、轻量 Tick、错误退避、单实例保护 |
| Drive System | 已完成 | social_need / curiosity / rest_need |
| Proactive Behavior | 已完成 | Candidate、评分、Cooldown、Budget、活跃会话保护、安静失败 |
| Scheduler | 已完成基础 | ScheduledContextEvent 结构与持久化；没有自然语言自动抽取 |
| Jev Decision Layer | 已完成基础 | off/shadow/hybrid/primary；Perception/Memory/Planner/Proactive/Media/Daily Event 接入 |
| Decision Metrics | 已完成本地持久化基础 | latency、fallback、confidence、disagreement、LLM avoided、usage；默认每 30 秒及正常卸载时保存，重载恢复 |
| Reactive Media | 已完成基础 | 旧索引复用、本地 VAD/能量/文本筛选、Jev 有界选择、保守表情语义对齐、Genie TTS、延迟表情发送 |
| Test-turn JSON | 已完成基础 | 默认关闭；逐轮原子 JSON 记录情绪分数、阶段诊断、有限表情候选资格摘要和交给框架的文本 |
| Segmented sending | 已完成基础 | Reactive/Proactive 默认启用，按可读边界拆分并按会话串行发送 |
| TencentDB | 未实现于当前工作区 | 不存在 Adapter 或 HTTP 集成 |
| Autobiographical Memory | 未完成 | SharedExperience 仅为基础结构 |
| Desktop/WebSocket/Live2D | 未实现 | 不在当前版本范围 |

## 4. 目录和边界

```text
astrbot_plugin_alice_agent/
├── main.py                       # 薄 AstrBot composition root
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── HANDOFF.md
├── adapters/
│   ├── astrbot/                  # AstrBot event/LLM/sender/filter/Genie renderer
│   ├── jev/                      # TypeSafe HTTP Adapter
│   ├── memory/                   # local_sqlite / in_memory Adapter
│   └── storage/
├── alice_agent/
│   ├── core/                     # Pipeline、Agent、SessionManager
│   ├── decision/                 # Jev-agnostic Decision Port/Service/Policy
│   ├── domain/                   # 统一领域对象
│   ├── drives/
│   ├── events/
│   ├── generation/
│   ├── llm/
│   ├── loop/
│   ├── memory/
│   ├── media/                    # Catalog、local scoring、Jev-aware selector
│   ├── perception/
│   ├── persona/
│   ├── planner/
│   ├── ports/
│   ├── proactive/
│   ├── prompts/
│   ├── relationship/
│   └── state/
└── tests/
```

边界约束：

- `alice_agent/` 禁止导入 `astrbot`。
- `alice_agent/` 禁止导入 `httpx`、TypeSafe SDK 或硬编码 TypeSafe endpoint。
- AstrBot API 只能位于 `main.py` 和 `adapters/astrbot/`。
- TypeSafe HTTP 只能位于 `adapters/jev/`。
- Core 只能依赖 Port 和领域模型。
- 外部服务失败不得破坏被动聊天。
- 主动行为失败必须优先安静。

## 5. AstrBot 接入流程

入口：`main.py`

插件构造阶段：

1. 读取 AstrBot 配置并构造 `AgentConfig`。
2. 用 `AstrBotLLMAdapter` 包装当前 AstrBot Provider，并在每次调用时读取该会话的备用模型列表。
3. 构造本地 SQLite Memory、Metadata、Relationship、Autonomy store。
4. 构造 Reactive Pipeline。
5. 构造 Agent Loop 和 AstrBot proactive sender。
6. Jev 启用且存在 Key 时才构造 `TypesafeJevAdapter`。

生命周期：

```text
initialize()
  → AgentLoop.start()

terminate()
  → AgentLoop.close()
  → AliceAgent.close()
  → Memory/State/Relationship/LLM/Decision resources close
```

消息 Handler：

- 使用私聊消息 Filter。
- 自定义 Filter 保留 AstrBot 唤醒前缀/命令行为。
- `AstrBotMessageAdapter` 将事件转换为 `AgentEvent`。
- 在任何可能等 session lock 的操作之前推进本机 reactive revision，并让 `AgentLoop.register_user_event()` 取消过期主动行为；随后把消息送入 pending FIFO，再更新 Drives。
- 可合并的纯文本在 0.8 秒安静窗内收集，首条起 3 秒为总硬期限（包含 Jev 分类时间）。直接问句/求助、明确结束或不合并安全的媒体消息跳过安静窗；媒体/不确定内容不跨事件合并。
- Core 生成在共享发送锁之外执行，并通过 revision guard 在阶段边界检查过期；只有最终媒体/发送阶段持有共享发送锁，发送前再次校验。
- 新输入使未交付旧生成失效；保留已接收用户文本供下一次重新生成。部分分段发送后被插话，只提交实际已经交给 AstrBot 的文本，不记录未发送后半段。Core 的 State/Relationship/Memory/会话历史也在实际交付时提交，避免生成但未发的回复成为对话事实。
- 最后 `yield` 的内容进入 AstrBot response stage，再 `event.stop_event()` 阻止默认 LLM 重复回复。AstrBot 接受 Generator 输出不代表平台确认送达。
- `response_timing_enabled=false` 为兼容回滚路径：单消息即时处理，不进行合并/等待/时机 Jev 调用；原有 Planner `wait/ignore` 静默语义仍保持。

### 模型回退修复（2026-10-11）

根因已对照本机 AstrBot 源码确认：`Context.llm_generate()` 只调用指定 Provider 的 `text_chat()`；常规聊天的备用模型切换位于 Agent Runner，Alice 的独立生成链路不会自动经过该逻辑。

`adapters/astrbot/llm_adapter.py` 现在通过 `context.get_config(umo=session_id)` 读取 `agent_runner.config.model.fallback_provider_ids`，先请求当前会话模型，失败后按列表顺序调用备用模型。模型抛出异常（含 Provider 自身抛出的超时）、返回 `role="err"` 或空白文本时均可继续回退；重复 ID、主模型 ID 和无效列表项会被排除。不存在或非聊天类型的备用 Provider 会由 AstrBot 调用接口报错，再继续下一项。成功结果中的 `LLMResult.model` 为实际使用的 Provider ID；主模型成功时只调用一次。

所有经同一 LLMService 的感知、规划、回复、主动生成、每日事件和 TTS 模型翻译均获得该能力，不额外运行 AstrBot Agent/工具调用，也不改写会话所选模型或运行配置。切换日志只包含 Provider ID 与异常类型，不复制错误正文、提示词或密钥。全部模型失败仍交由各阶段原有失败策略处理。

`llm_timeout` 仍是每次 LLMService 调用的整条模型尝试链总时限，默认 30 秒，不是每个模型各享 30 秒。若主模型一直等待直到耗尽总时限，则整条调用取消，没有剩余时间尝试备用模型；卸载或主动取消同样不会继续调用备用模型。本次没有延长总时限、改变 SDK 内部重试或增加 Jev 调用。

本地验证包括：先用新增测试复现原实现失败，再验证顺序回退、错误/空输出、去重、会话配置隔离/更新、全部失败、取消及总超时；调用本机真实 `Context.llm_generate` 方法、仅模拟网络 Provider，验证主模型和不存在的备用模型失败后使用后续备用模型；模拟私聊 Handler 验证感知、规划、回复三个阶段都回退并 yield 回复。全量 225 项通过；未调用真实付费模型、未做 QQ 平台送达验收、未重载运行中的插件。源码修复需重载 Alice Agent 后生效。

## 6. Reactive Pipeline 实际流程

核心入口：`alice_agent/core/pipeline.py::AgentPipeline.handle_event()`

```text
AstrBot event
  ↓
AstrBotMessageAdapter
  ↓
AgentEvent + pending message boundary
  ↓
Reactive revision advance + loop activity registration
  ↓
Local / optional Jev timing classification (bounded by first-message deadline)
  ↓ quiet-window FIFO merge of safe text only
  ↓
Alice Core pipeline with stale-revision guard (no send lock)
  ↓
State BEFORE
  ↓
Decision-aware Perception
  ├── off: existing Perception
  ├── shadow: Jev + existing，使用 existing
  ├── hybrid: high Jev，否则 existing LLM
  └── primary: confidence ≥ 0.80 Jev，low/complex 使用 existing LLM
  ↓ low-confidence Jev numeric + LLM, then blend with local VAD (selected reply affect)
  ↓
StateReducer update
  ↓
RelationshipReducer update
  ↓
Memory query + local SQLite recall
  ↓
Metadata merge
  ↓
Jev Memory relevance（可选、一次批量请求）
  ↓
Code relevance gate + rerank
  ↓
Decision-aware Planner
  ↓
Planner selected memory IDs
  ↓
Memory access tracking
  ↓
ResponseGenerator / AstrBot LLM Provider
  ↓
Local media rank + optional Jev media choice
  ↓ preview-only state/relationship/memory effects
AgentResponse
  ↓
shared send lock + final revision check
  ↓ local media/render/yield
actual yielded text/audio confirmation at adapter boundary
  ↓
commit_delivery(actual delivered text)
```

本地 VAD 默认开启，对当前用户消息调用 `POST {vad_endpoint}/vad`（默认 `http://127.0.0.1:8001`）。原始 `vad` 三轴按 1..5 保存并归一化为 0..1；成功时和可用 Jev 数值逐轴混合，所选情绪 V/A/D、粗粒度标签及下游 State/Planner/Response/Media 输入均使用混合值。失败、超时或分数无效时按可用的 Jev／LLM Perception 回退，不写伪造本地 VAD 分数。

若全局 Jev 与 Jev Perception 均启用，Jev 同一次 Perception 请求会多评估 V/A/D 和情绪强度、温暖度、紧张度、压抑度、情绪目标/变化及情感回应需要；不会因此再发第二个 Jev 请求。Jev 获得最多三轮上下文，本地 VAD 只看当前消息，两者输入范围不完全一致。Jev 整体或某轴 confidence <0.80 时先按轴混合 LLM 数值（Jev 权重为 `0.5 × confidence / 0.80`，confidence 为整体和该轴的较小值），然后与本地 VAD 按 `jev_vad_weight` 混合，默认各半。没有有效 LLM 轴值时该轴仅用本地 VAD。`combined` 是实际回复情绪，不再只是诊断，且在 `shadow` 中同样生效；分类/行为决策仍不受 shadow Jev 影响。低置信度可能增加一次 LLM 感知调用；本公式未校准，不构成准确率提升结论。Jev `api_latency_ms` 含网络往返，缓存命中另记录本轮查找时间。本次未做真实模型质量或 QQ 送达验收。

本机 VAD HTTP Adapter 已用合成文本核对响应与归一化，并通过 localhost VAD + fake Jev/LLM/event 的端到端 smoke：分数进入下游、组合公式正确、日志开关生效。该验证不是模型准确率评估，没有调用 Jev API 或 QQ 发送，也未改动运行配置。

AstrBot Adapter 在 Pipeline 返回后执行媒体 I/O：若选择语音则按 Genie 协议切换参考音频、必要时调用当前会话 Provider 翻译并合成 WAV；若选择表情则在文本/语音结果发送后延迟发送图片。媒体任一阶段失败都不会替换或中断文本回复。Reactive 与 Proactive 分段发送默认开启（间隔默认 1 秒），每个完整句子单独发送，不按字符数截断句子；无句号的长句和完整代码块可能超过旧的 120 字目标值。分段拼接与原文本一致。QQ 官方平台语音独立成一条 Record 消息，再逐句发送文本；其他平台仍将语音随首段发送。表情只在最后一段后发送；部分失败不重发整条回复，同一会话发送序列共用锁。若 AstrBot 全局 segmented-reply 被配置为再次处理一般消息，它可能对插件输出二次拆分；Alice 不改动全局设置。

`test_log_enabled` 默认关闭。开启后，`data/plugin_data/astrbot_plugin_alice_agent/test_turns/` 每条私聊写一个原子 JSON，包含用户/生成回复/实际 yield 文本、有限近期对话、本地 VAD／Jev／LLM／混合情绪分数（可用时）、Jev/VAD 参数与延迟、Perception/State/Relationship/Memory/Planner/Media 摘要和发送结果。若同轮感知或规划阶段同时运行 Jev 与 LLM，顶层 `decision_comparison.perception/planner` 保存双方决策及实际采用来源；未同时运行则对应值为 null。`delivery` 只证明已交给 AstrBot，不代表 QQ 平台确认送达。不记录配置对象或 API 密钥。日志含用户标识、私聊文本和记忆摘要，必须按敏感数据管理。关闭后不再新增且保留旧文件；terminate/reload 会阻止旧实例的待处理写入。

### 回复时机（v0.8.0）

- `response_timing_enabled=true` 默认启用本地规则和受限时机判断；`response_wait_quiet_seconds=0.8`，`response_wait_max_seconds=3.0`。硬期限从 batch 首条输入计时，包含 Jev 分类，不会因新消息续期。
- 普通、可合并的纯文本先等待安静窗；常见直接问句/求助、本地规则识别的结束语，以及含媒体/不确定内容的消息跳过安静窗。跳过安静窗不等于零延迟：若全局 Jev 可用，即使在 `shadow` 也会执行时机观察调用，可能增加延迟；最终仍采用本地规则。硬期限到达时，未完成分类取消并按本地规则回退。
- “不用回复”只有精确、保守的结束指令形式才会忽略；明确“等我说完”会优先等待，问句/求助不会因短或 Jev 的忽略判断而被吞。语言规则覆盖常见显式表达，不保证识别所有中文语用。
- Jev `shadow` 不改变本地选择；全局 `hybrid/primary` 中，达到当前 DecisionPolicy 置信度要求的模型输出只可能影响普通陈述的时机，不能覆盖明确 no-reply、显式等待、结束边界或受保护的问句。
- 仅合并连续且 message adapter 确认安全的纯文本，diagnostics/test_turn 记录有序原文边界。新输入在等待 Core/session lock 前推进 reactive revision；旧生成如果还未交付会被丢弃并基于保留输入重生成。生成不占共享发送锁，发送阶段再次 guard。
- Core 将 State/Relationship/Memory/会话 History 的提交延至 adapter 确认已交给框架的文本；分段途中被新输入中断，只提交已 yield 的实际部分。平台是否最终送达仍未知。关闭 timing 会退回旧单条即时链路，不合并、不等待、不运行 timing classifier，且保留 Planner `wait/ignore` 原有静默语义。

单模块失败策略：

| 阶段 | 失败行为 |
|---|---|
| State read | 使用默认 `AgentState` |
| Perception | 使用 `PerceptionResult.fallback()` |
| State update | 保留旧 State |
| Relationship | 尝试读取旧值，失败再使用默认值 |
| Memory recall | `memories=[]` |
| Planner | `ActionPlan.fallback()` |
| Memory selection/access | 清空 selected memory，继续回复 |
| Response | 返回配置中的友好 fallback 文本 |
| Memory observe | 记录异常，不影响已经生成的回复 |

### 小说资料与对白（v0.9.0接入，随v0.9.1发布）

`ContextBuilder` 启动时加载 `alice_agent/knowledge/data/` 的只读JSONL，不将小说批量写入用户数据库。Planner获得相关剧情背景；Response的实际LLM请求另获得 `character_knowledge.dialogue_examples`，包含具体场景、原句、原受话者、来源与限制，与 `selected_memories/recent_history` 分开。

默认每轮最多3张剧情卡、3条对白，新增JSON最多6000字符。有具体触发语且action兼容才提供对白；“关心、解释、确认、日常聊天”等宽泛标签不参与召回，也不放进给回复模型的示例上下文。缺失、损坏或无匹配时回退空结果。没有额外LLM/Jev/embedding调用，但已有请求的上下文token会增加。

默认 `integrity_knight` 只使用13条骑士阶段对白、89张骑士剧情卡，以及44张童年背景卡。童年卡统一为 `reader_background`；骑士时期听闻童年的记录也不恢复童年亲历记忆。后期15条对白和95张剧情卡默认禁用，需显式设置 `later_canon`；不是严格逐章时间线系统。自定义每日事件种子、默认模板及LLM扩写均防止虚构童年回忆。

根据用户全章覆盖版压缩包筛选，不导入补篇、全文、未确定事项或译者评论。剔除威胁式手帕台词和空泛确认句，修正冻结中贝尔库利的“临终”误标。来源哈希、筛选条件及字面核对报告均保留；资料语义和真实模型角色质量仍不能由这些核对直接证明。当前未发布或重载生产实例，完整配置与复现见 `docs/NOVEL_CONTEXT.md`。

## 7. Domain Models

主要领域对象：

- `AgentEvent`
  - event_id
  - trace_id
  - type / event_type
  - source
  - session_id
  - user_id
  - timestamp
  - text
  - payload
  - metadata
- `PerceptionResult`
  - intent/topic/emotion/valence/arousal
  - 8 个 social signals
  - importance/memory_candidate/confidence
- `AgentState`
  - mood_valence/mood_arousal/energy/activity/current_topic/conversation_mode
- `MemoryItem`
  - 统一内容、层级、retrieval、metadata 和 access 字段
- `RelationshipState`
  - familiarity/trust/closeness/interaction timestamps
- `DriveState`
  - social_need/curiosity/rest_need
- `ActionPlan`
  - action/tone/length/directness/follow-up/memory IDs 等
- `AgentResponse`
  - text/trace/action/metadata
- `CandidateAction`
  - action ID、原因、评分维度、来源事件、有效期和状态

`session_id` 和 `user_id` 始终分离：

- State、History、Drive、session lock 按 session 隔离。
- Relationship 按 user 隔离。
- Memory 默认 `local_memory_scope=user`，因此同一用户可跨 session recall。
- 配置为 `session` 时，长期 Memory 完全按 session 隔离。

## 8. State 与 Relationship

### State

- v0.8.3 的 `session_persistence_enabled=true` 默认保存心境和有界近期对话；关闭后，心境仅在每日事件仍启用时保持既有 SQLite 持久化，近期 History 使用内存。
- 使用 `StateReducer`，LLM/Jev 都不能直接设置数值。
- 每次用户事件在 session lock 内更新。
- `TimeStateReducer` 让 valence/arousal/energy 随时间回归 baseline；v0.8.4 增加 valence 每小时 0.02 向零回归，不依赖每日事件开关。
- AgentLoop 以最后一次用户交互计算累计闲置，避免把单次 30 秒 Tick 当成全部闲置时间。累计 15 分钟退出 `chatting`；累计至少 30 分钟且满足休息需求 ≥0.60、精力 ≤0.35 之一时进入 `resting`。休息需求 ≤0.20 且精力 ≥0.60 后回到 `idle`；新用户文本按原 StateReducer 进入 `chatting`。关闭 rest drive 时，旧的休息需求不参与状态进入/退出判断。规则描述的是角色状态，不是用户睡眠或锁屏信号。
- `DailyMoodReducer` 在换日时使 V/A/D、精力、压力和孤独感向基线渐进恢复，并叠加有界事件影响；每日事件按 session 隔离，重启后不重复施加。

### 会话连续性（v0.8.3）

`SessionManager` 经 `StoragePort` 在 `alice_sessions.db` 保存按 session 隔离的短期对话快照；StateManager 使用既有 `alice_mood.db`。首次需要某会话历史时懒加载，在感知、回复时机和 AgentLoop 的每日事件／主动生成前完成恢复；使用单独的历史 I/O 锁避免与现有会话锁重入。

默认 `history_limit=8`、`history_restore_max_age_hours=72`（1–168 小时）。磁盘快照最多 64 轮，每轮用户和助手文本各截到 8000 字符。恢复验证 schema、字段类型和有限时间戳，剔除过龄、未来异常和损坏条目；允许平台消息时间最多比本机领先 5 分钟。恢复的内容仍通过现有 history 提示字段传递。时限是上下文使用期限，没有全局清理沉睡 session 的历史行。

AstrBot 路径在生成时传入 `is_current`，由 Pipeline 的 `commit_delivery` 与主动发送的 `on_delivered` 写入实际已交给框架的文本；分段中断保留实际部分，尚未交付的生成不保存；明确不回复仍可记为用户输入／助手 null。独立 Core 调用 `handle_event` 未传 `is_current` 时保持既有即时提交约定，不能把这一调用等同于平台送达。历史存储失败不阻断聊天，AstrBot 日志只报告错误类型。初次读取失败时本次实例仅保留该会话内存，防止覆盖尚未读取的旧快照，重载后再恢复；写入失败可在下次提交重试。心境与历史不是同一事务，断电时可能只完成其中一项。关闭会话连续性不读取或新增快照，不删除旧文件；`test_log_enabled` 是独立诊断开关。

不导入旧诊断或长期 Memory 原始轮次，不增加模型调用。新功能需重载插件后才进入运行进程；本地重建插件实例的验证不能证明真实 QQ 平台送达。

### 每日事件（v0.7.0，可选）

- `daily_events_enabled=false` 为默认值。启用后，Reactive 和 AgentLoop 在会话锁内检查本地日期；每会话每日生成 0–3 个小事件，默认上限 2。
- 事件骨架由代码选取，可参考前一天事件作有限延续；同会话近期 History（包括有效恢复记录）可提供前 1–3 天用户对话线索。实际仍受最近轮数与恢复期限限制。不会从不存在的 TencentDB 或系统日程读取事件。
- 可用当前 LLM 将骨架扩写为短句；无效输出或失败回退模板。Jev 可作六轴有界反应评估，`shadow` 仍使用代码值；在 `hybrid/primary` 中，整体或单轴置信度低于 0.80 时尝试 LLM 评分并仅混合低置信轴，LLM 无效或失败时回退代码值。此路径最多额外增加一次 LLM 评分调用，代码限制每日单轴总影响为 ±0.30。
- 今日事件文本及话题线索进入 State 供回复参考，不要求每轮提及；`memory_candidate` 只是标记，尚未自动写长期 Memory。
- `persona_profile` 默认提供整合骑士爱丽丝的虚构背景、兴趣、作息与事件种子；ContextBuilder 注入回复/Planner/主动规划上下文，DailyEventService 使用兴趣生成小事件骨架。只对完全匹配旧普通生活默认值的画像做内存迁移，有意自定义保留，不改写实例配置。不得把设定写成用户事实、现实观测或实时日程，也不能用兴趣字段覆盖童年记忆边界。
- 事件批次先存入 `alice_mood.db`，再保存心境；若两步之间中断，下次复用事件批次。LLM/Jev 调用占用会话锁，开启前需评估延迟、费用和隐私。

### Relationship

- 长期、按 user 隔离。
- SQLite 持久化。
- familiarity/trust/closeness 范围为 `0..1`。
- 默认单轮最大变化 `0.02`。
- interaction_count 每次有效交互增加。
- LLM/Jev 只能提供事件判断，不能设置关系数值。

## 9. Memory 系统

### 当前后端

生产默认：`local_sqlite`  
测试可用：`in_memory`

本地数据库：

```text
data/plugin_data/astrbot_plugin_alice_agent/
├── alice_memory.db
├── alice_intelligence.db
├── alice_autonomy.db
├── alice_mood.db
├── alice_sessions.db
└── alice_metrics.db
```

职责：

- `alice_memory.db`
  - 原始对话轮次
  - 可召回长期 Memory 内容
- `alice_intelligence.db`
  - Memory Metadata
  - Relationship
  - SharedExperience
- `alice_autonomy.db`
  - Drive
  - session revision
  - ScheduledContextEvent
  - cooldown send records
  - action status
- `alice_mood.db`
  - 会话心境（会话连续性或每日事件启用时）
  - 每日事件批次（每日事件启用时）
- `alice_sessions.db`
  - 有界近期对话快照（会话连续性启用时）
- `alice_metrics.db`
  - 按决策任务类型累计的 Jev 汇总指标（默认开启，不包含私聊原文、用户标识和密钥）

### Recall 流程

```text
MemoryQueryBuilder
  ↓
LocalSQLiteMemoryAdapter recall
  ↓
MemoryItem normalize
  ↓
Metadata merge / conservative initialization
  ↓
Optional Jev relevance judgment
  ↓
hard retrieval relevance gate
  ↓
Code reranker
  ↓
Top memory_recall_limit
  ↓
Planner selection
  ↓
Top memory_selected_limit to ResponseGenerator
```

默认 Code Reranker：

```text
effective_relevance =
    retrieval_relevance * (1 - jev_memory_relevance_blend)
  + jev_relevance       * jev_memory_relevance_blend

final_score =
    effective_relevance * 0.65
  + importance          * 0.15
  + emotional_weight    * 0.07
  + relationship_weight * 0.08
  + recency             * 0.05
```

强约束：

- `retrieval_relevance < memory_min_relevance` 时先拒绝。
- Jev 或 importance 不能让完全无关的 Memory 越过硬 gate。
- access_count 只记录使用，不自动提高 importance。
- pinned 不等于每轮召回，仍必须相关。
- 默认 INFO 不打印完整 Memory；DEBUG 只打印 ID、分数和截断摘要。

## 10. Event Bus 与 Agent Loop

EventType 当前支持：

- USER_MESSAGE
- TIME_TICK
- SESSION_STARTED
- SESSION_ENDED
- USER_IDLE
- USER_RETURNED
- MEMORY_EVENT
- STATE_EVENT
- SYSTEM_START
- SYSTEM_SHUTDOWN

EventBus 使用 recent event ID cache 防止短期重复处理。

Agent Loop：

- `start()` 幂等。
- `stop()` 取消后台任务。
- 默认 Tick 间隔 30 秒。
- 每日事件关闭时，普通 Tick 不调用 LLM 或 Jev；启用后，每会话换日检查可能调用每日事件 LLM/Jev。
- Tick 异常不会结束 Loop。
- 连续异常执行指数退避，最高 300 秒。

每个 Tick：

```text
maintenance
  ↓
due scheduled contexts
  ↓
TIME_TICK events
  ↓
per-session lock
  ↓
TimeStateReducer
  ↓
DriveReducer
  ↓
active conversation check
  ↓
CandidateAction generation
  ↓
cooldown / daily budget
  ↓
ActionScorer
  ↓
只有 allow 后才 recall Memory / Jev / Planner / Response LLM
```

## 11. Drive System

Drive 与 Emotion、Relationship 分离。

### social_need

- 随时间以饱和函数增长。
- closeness 只小幅影响增长率。
- 用户发消息后减少 `0.22`。
- 高 social_need 不代表必须发送消息。

### curiosity

- 仅存在 unresolved/scheduled context 时增长。
- 用户发消息后减少 `0.08`。
- 当前没有从自然语言自动创建 schedule。

### rest_need

- 非 resting 状态随时间和 mood_arousal 增长。
- resting 时下降。
- 高 rest_need 降低主动行为分数。

所有 Drive 都 clamp 在 `0..1`。

## 12. Proactive Behavior

CandidateAction 类型：

- DO_NOTHING
- STATE_ONLY_UPDATE
- PROACTIVE_MESSAGE
- MEMORY_FOLLOW_UP

Action 状态：

- candidate
- planned
- executing
- completed
- cancelled
- expired

主动评分默认公式：

```text
score =
    social_need        * 0.18
  + curiosity          * 0.24
  + relationship       * 0.15
  + context_relevance  * 0.55
  + urgency            * 0.20
  - interruption_cost  * 0.15
  - rest_need           * 0.08
```

主动消息前必须通过：

1. active conversation window
2. candidate validity/expiry
3. global/per-user cooldown
4. same-topic cooldown
5. rolling 24h daily budget
6. ActionScorer threshold
7. Memory recall
8. Jev Proactive Gate（启用时）
9. Proactive Planner fallback
10. session revision validity
11. Response generation
12. send 前再次检查 revision

默认限制：

- 主动阈值：`0.72`
- defer 阈值：`0.60`
- 最短间隔：6 小时
- 同话题间隔：24 小时
- 每用户滚动 24 小时预算：1 条
- 用户消息后活跃保护：15 分钟

可选每日生活分享候选依赖 `daily_events_enabled=true`，仅从当日已生成事件中选取；本地时间默认 09:00–21:00。每会话每天最多 2 次日常事件的模型规划尝试（每个 Tick 仍会做轻量候选评估），且仍受活跃保护、评分、Jev gate、冷却及原有每天最多 1 条的 proactive budget。这个候选不会强制每天发送；每日事件总开关 schema 默认 false，2026-09-29 历史实例记录为 true，当前实例值需另行核对。

用户新消息会：

- 发布 USER_MESSAGE event。
- 增加 session revision。
- 取消 pending proactive action。
- 使已经生成但尚未发送的旧主动行为失效。

任何 Memory/Planner/Response/Jev 失败都不会发送随机 fallback 主动消息。

## 13. Jev Decision Layer

### 官方接口

- `POST https://api.typesafe.ai/v1/systemone`
- `GET https://api.typesafe.ai/v1/models`
- Bearer authentication
- Question 类型：Choice / Noul / Score
- 默认 model alias：`jev-latest`

HTTP Adapter 使用持久化 `httpx.AsyncClient`。Key 来源：

1. AstrBot `jev_api_key` 密码字段
2. `TYPESAFE_API_KEY`
3. `JEV_API_KEY`

推荐环境变量，不要写入源码。

### 工作模式

| 模式 | 行为 |
|---|---|
| off | 完全不调用 Jev |
| shadow | 分类/行为决策使用原路径；回复情绪是例外，使用 Jev 与本地 VAD 混合 |
| hybrid | 达到对应策略置信度门槛时使用 Jev，否则升级原有路径 |
| primary | 达到对应策略置信度门槛时使用 Jev，低置信度/复杂/失败升级原有路径 |

Proactive 是例外：即使 `primary`，也只接受高置信度；不确定时 DO_NOTHING。

实际 Jev 决策采用下限为 0.80（`hybrid` 高置信门槛默认 0.85）。Perception、Planner 有既有 LLM 回退，每日事件在非 shadow 模式下有有界 LLM 评分；Memory、Media、回复时机与 Proactive 无独立 LLM 判断器，低置信继续走各自的本地／安全策略。不能笼统声称所有 Jev 低置信路径均调用 LLM。

默认：

- `jev_enabled=false`
- `jev_mode=shadow`
- auto threshold `0.85`
- review threshold `0.80`（旧实例即使显式配置更低，实际采用下限仍为 0.80）
- timeout `2500ms`

### Schema 版本

- perception_v2（增加语义情绪评分字段）
- memory_relevance_v1
- planner_v1
- proactive_v1
- media_v1
- response_timing_v1

Cache key 包含：

- model
- decision_type
- state_hash
- schema_version
- question schema

### Jev Perception

判断：

- intent Choice
- emotion Choice
- 8 个独立 social-signal Noul
- memory_worthy Noul
- 5 级 importance Score
- relationship event Choice
- requires_deep_reasoning Noul
- V/A/D score，以及情绪强度、温暖度、紧张度、压抑度、情绪目标/变化、需要情感回应（raw Score 0..4，诊断归一化为 0..1）

这些 Jev 情绪分数与本地 VAD 融合为实际回复情绪；低于 0.80 的数值先与有效 LLM 数值融合。若本地 VAD 不可用，按可用的 Jev／LLM Perception 回退。intent、relationship 等分类判断仍按原模式参与；低置信度 social signal、importance 也可先融合 LLM 数值。

Jev Perception 当前 topic 固定为 `general`。复杂话题抽取应升级原 LLM Perception。

### Jev Memory

- 多个候选在一次 System One 请求中判断。
- 只发送 current message/topic、memory ID、截断内容和 type。
- 不发送完整 Metadata。
- Jev relevance 只作为 Code Reranker 的输入。

### Jev Planner

合法 Action：

- reply
- acknowledge
- comfort
- clarify
- tease
- recall_then_reply
- wait
- ignore

同时判断：

- use_memory
- memory_choice
- ask_followup
- tone
- response_length
- directness
- requires_deep_reasoning

Jev 不生成最终文本，也不生成自由文本 response_goal。

### Jev Proactive Gate

Choice：

- SEND
- DEFER
- DO_NOTHING

SEND 还要求：

- SEND probability 达到 auto threshold
- natural_reason_exists 达标
- likely_to_interrupt 足够低
- relevant_context_exists 达标
- appropriateness 至少 0.75
- requires_deep_reasoning 低
- 所有 Code cooldown/budget/policy 已通过

### Jev Media

本地代码先按以下信号为音频候选选出 Top K：

- Perception valence/arousal
- AgentState mood/energy
- ActionPlan energy/directness/tone/expression
- 当前消息与回复对候选描述的本地文本相关度

表情使用助手最终回复作语义资格判断，不用用户消息、Planner 标签或 VAD 单独触发表情；符合语义的候选再按本地分数排序、取 Top K 并通过 emoji score threshold。这样可避免仅因用户提到离别、或当前情绪值高就发出无关表情。

Jev 只做有界问题：

- send_voice Noul
- voice_choice Choice
- send_emoji Noul
- emoji_choice Choice
- requires_deep_reasoning Noul

发送给 Jev 的候选包含 ID、截断描述、VAD、energy 和本地分数，不包含文件路径或音频内容。v0.8.1 对通过表情语义筛选的候选还会附带 intent/tag/caption 元数据；这是既有 Jev Media 请求的 payload 扩展，不增加请求次数。`shadow` 的媒体选择始终采用本地结果；`hybrid` 只采用高置信度结果；`primary` 仅在 confidence ≥0.80 时采用 Jev 结果。失败、低置信度或复杂判断回退本地选择。

v0.8.1 在本地候选决策中增加有限的表情语义对齐：使用最终回复文本中的规则识别信号，与索引中的意图、标签、配文和描述匹配；没有合格候选时允许 abstain。它不检查图片像素，也不是 LLM/视觉模型，不能保证中文语义或表情文化语境准确。`test_turns` 可记录候选 ID、匹配类别、资格原因和分数的有界摘要；逐候选明细最多 12 项，实际总数与语义合格数独立记录，不保存这项摘要中的私聊原文和路径。`abstained` 等于语义合格数为零（筛选后、Top K 前）；筛后 Top K 候选还须通过本地 emoji score threshold，Jev 不能重新引入未通过的素材。媒体开关、发送概率、冷却和后续策略仍可能阻止最终发送。

索引可选提供 `intent`、`tags`、`caption`；旧格式 `desc` 通过有限模式与配文词表作兼容解析。2026-09-29 当前 `emoji_index.json` 有 101 条旧格式记录，包含 `path`、`desc`、VAD 和能量字段，但没有显式语义元数据，因此新功能无法从文件名或图像像素推断未写在描述中的语义。

### Reactive Media 实际 I/O

- 默认 `media_enabled=false`，避免旧 `deeplx_genie_tts` 仍启用时重复发送。
- 2026-09-29 当前实例只读配置为 `media_enabled=true`、`media_emoji_enabled=true`、`test_log_enabled=true`、Jev `shadow`；这不是 schema 默认值。此次源码更新未重载插件，因此不改变当前运行进程。
- 索引/素材配置留空时，自动复用同级旧插件的 `audio_index.json`、`emoji_index.json`、`audio/`、`images/`。
- 同时读取旧插件配置作为新字段为空时的兼容回退；旧监听地址 `0.0.0.0` 在客户端侧转换为 `127.0.0.1`。
- Genie 使用 `/load_character`、`/set_reference_audio`、`/tts`，持久化 `httpx.AsyncClient`。
- 日文/英文 TTS 优先使用 DeepL 官方 API（配置 `media_deepl_api_key` 或 `DEEPL_API_KEY` 后启用）；无密钥、接口失败或结果无效时回退当前 AstrBot 会话 Provider。不复制旧插件的非官方 DeepL 免费接口。
- v0.8.4 起 `media_log_tts_inputs=false` 默认关闭 TTS 输入日志。显式开启并重载后，AstrBot 运行日志 `Alice media: TTS inputs` 才记录实际参考音频路径、参考文本、处理后的原文和最终译文；与 `test_log_enabled` 独立，关闭不删除已有运行日志。
- RIFF WAV 原样保存；原始 PCM 封装为 32kHz/mono/16bit WAV。
- 语音与文本同一消息链发送；表情默认延迟 0.5 秒。
- 媒体只装饰 Reactive 回复；Proactive sender 仍发送纯文本，但长内容会分段。
- 新插件不含索引构建命令，索引更新仍依赖已有文件或外部预处理。

### Decision Trace 与 Metrics

Trace：

- decision_id
- trace_id
- decision_type
- schema_version
- configured/resolved model
- input_hash
- latency
- answers/probabilities
- confidence
- mode
- token usage

默认不记录完整用户文本或完整 Memory。

Metrics 汇总：

- calls
- latency_avg_ms
- errors/timeouts
- fallbacks/fallback_rate
- high/medium/low confidence
- llm_escalations
- existing_comparisons
- disagreements/disagreement_rate
- llm_calls_avoided
- input/output tokens

v0.8.4 默认 `decision_metrics_persistence_enabled=true`，经 `StoragePort` 保存到 `alice_metrics.db`，每 30 秒及正常卸载时写入；卸载等待已开始的保存结束再写最终快照。首次读取通过 schema、类型和有限非负数校验恢复汇总，坏桶跳过；读取失败时本实例保持内存统计且不覆盖旧记录，写入失败可在下一次保存重试。关闭开关不读写或删除旧指标。

只保存按任务类型累计的计数、累计延迟和 token 数；派生比例按恢复后的计数重新计算。没有用户标识、私聊文本、提示词、逐轮决策或密钥。指标尚未按模型/模式/日期分组，也没有管理面板；不能直接用于模型质量或实际费用结论。异常退出可能损失最近一次保存之后的计数，保存间隔并不是故障情况下的严格损失上限。

## 14. Context 与 Prompt 边界

Prompt 文件：

- `alice_agent/prompts/perception.txt`
- `alice_agent/prompts/planner.txt`
- `alice_agent/prompts/proactive_planner.txt`
- `alice_agent/prompts/response.txt`

Persona 文件：

- `alice_agent/persona/identity.txt`
- `alice_agent/persona/personality.txt`
- `alice_agent/persona/speaking_style.txt`

ContextBuilder 按阶段构建上下文：

- Perception：当前消息 + 少量近期历史。
- Planner：事件、Perception、State、Relationship summary、候选 Memory、少量历史。
- Response：Persona、ActionPlan、State、当前消息、Planner selected Memory、近期上下文。
- Proactive：Candidate、Drive、Relationship、Availability、相关 Memory。

v0.8.1 回复风格提示由本地规则从当前消息组与最近 16 轮中最多 4 条非空、实际已交付回复中生成。它只添加固定的详略/话题线索、重复通用开场和连续问句提示，不把这些原文复制进提示，不增加 LLM/Jev 调用。详略/话题线索来自有限词语模式，可能误判叙述；Prompt 要求模型先核对当前消息原文。提示不能覆盖用户当前要求或 Planner action，也不能保证模型每次都遵守。

不要把所有信息重新合并为一个巨大 Prompt。

## 15. 并发与有效性

- 使用 per-session lock；不同 session 可并发。
- State/Drive/用户消息更新在同一 session 内串行。
- RelationshipManager 使用 per-user lock。
- 不使用一个覆盖所有用户的全局业务锁。
- 长 LLM 阶段后使用 session revision 检查主动行为是否仍有效。
- 用户消息优先于过时主动消息。
- Reactive message intake 在任何等待活动/session lock 的操作前推进 revision 并提交到 pending FIFO；时机等待与 Core 生成都不持有共享 send lock，仅 final media/yield 使用此锁。
- 每个 session 的 reactive queue FIFO；新消息会使旧未发送 response stale。等候或分类不能无限延长首条消息的 hard deadline；卸载时 cancel_all 唤醒 batch/classifier waiter 并取消任务。
- 只允许纯文本安全合并；媒体/不确定消息独立保留边界。局部 delivery guard 在各句发送前运行，插话不会继续发送过期的剩余段落。

## 16. 日志与隐私

v0.8.4 增加 `test_log_retention_days`（默认 0，不自动删除）。设为正数且诊断开启时，在新增 JSON 后最多每小时清理一次过期文件，按文件修改时间判断；仅匹配诊断目录内插件生成的普通 JSON 文件，不递归、不跟随符号链接、不清理 AstrBot 运行日志。关闭 `test_log_enabled` 后不执行清理。关闭日志开关不等于清除历史记录。

INFO：

- Reactive Pipeline 阶段和数量
- Agent Loop start/stop
- 成功主动发送
- 重要错误

DEBUG：

- 详细 Perception/State/Plan
- Memory ID、分项分数和截断摘要
- Drive/score/cooldown/do-nothing 原因
- Jev answers/probabilities/confidence/input hash

禁止记录：

- API Key
- Token/密码
- 完整私人配置
- 默认完整 Memory 内容

注意：Jev 的 schema 默认关闭，但最近核对的本机实例显式启用且处于 `shadow`；新回复时机服务在全局 Jev 可用时也会发起受限观察请求，包括 shadow。`shadow` 不改变时机或媒体选择，但 Jev／本地 VAD 的情绪混合是例外；会增加外部上下文发送与延迟/费用。不得把实例状态当成默认值，也不要在文档或日志中暴露凭据。

## 17. 配置默认值摘要

### Reactive

| 配置 | 默认值 |
|---|---:|
| enabled | true |
| debug | false |
| llm_timeout | 30s |
| history_limit | 8 |
| session_persistence_enabled | true |
| history_restore_max_age_hours | 72（1–168） |

### Memory

| 配置 | 默认值 |
|---|---:|
| memory_backend | local_sqlite |
| memory_recall_limit | 5 |
| memory_selected_limit | 3 |
| memory_importance_threshold | 0.60 |
| memory_min_relevance | 0.12 |
| local_memory_scope | user |
| local_memory_capture_all_turns | true |

### Relationship

| 配置 | 默认值 |
|---|---:|
| relationship_enabled | true |
| relationship_max_delta_per_turn | 0.02 |

### Agent Loop

| 配置 | 默认值 |
|---|---:|
| agent_loop_enabled | true |
| agent_tick_interval | 30s |
| proactive_enabled | true |
| proactive_score_threshold | 0.72 |
| proactive_defer_threshold | 0.60 |
| proactive_min_interval | 21600s |
| proactive_same_topic_interval | 86400s |
| proactive_daily_budget | 1 |
| active_conversation_window | 900s |

### Reactive reply timing

| 配置 | 默认值 |
|---|---:|
| response_timing_enabled | true |
| response_wait_quiet_seconds | 0.8s |
| response_wait_max_seconds | 3.0s |

### Persona and daily sharing

| 配置 | 默认值 |
|---|---:|
| persona_profile | JSON 整合骑士虚构画像 |
| proactive_window_start / end | 09:00 / 21:00（本地时间） |
| proactive_daily_attempt_limit | 2 次/会话/日（只限制日常事件规划尝试） |
| proactive_daily_score_threshold | 0.35（旧实例可能仍为0.45） |
| proactive_daily_probability | 0.55（基础值，受心情/事件/模型影响） |
| proactive_daily_probability_llm_enabled | true（每天每会话至多1次新增判断） |
| daily_events_enabled | false |

每日生活分享候选只从每日事件服务已有的当日事件中挑选；`daily_events_enabled` 未开启时没有该类候选。每日 attempt limit 是评分与 cooldown 通过后、Memory/Jev/Planner 之前的规划尝试上限；AgentLoop 每个 Tick 仍会做轻量评估。已发送仍受 `proactive_daily_budget` 默认 1 条限制，不保证每日有消息。

### Jev

| 配置 | 默认值 |
|---|---:|
| jev_enabled | false |
| jev_mode | shadow |
| jev_model | jev-latest |
| jev_timeout_ms | 2500 |
| jev_auto_threshold | 0.85 |
| jev_review_threshold | 0.80（实际采用下限） |
| jev_memory_relevance_blend | 0.25 |

### Daily Events

| 配置 | 默认值 |
|---|---:|
| daily_events_enabled | false |
| daily_event_max | 2（0–3） |
| daily_event_llm_enabled | true（仅总开关启用时有效） |
| daily_event_jev_enabled | true（还需全局 Jev 启用且有凭据） |

### Semantic VAD, diagnostics, and segmented sending

| 配置 | 默认值 |
|---|---:|
| vad_enabled | true |
| vad_endpoint | http://127.0.0.1:8001 |
| vad_timeout_ms | 5000 |
| jev_vad_weight | 0.5（实际回复情绪混合权重） |
| test_log_enabled | false |
| test_log_retention_days | 0（不自动删除） |
| media_log_tts_inputs | false |
| decision_metrics_persistence_enabled | true |
| segmented_send_enabled | true |
| segmented_send_max_chars | 120 |
| segmented_send_interval_seconds | 1.0 |

### Media

| 配置 | 默认值 |
|---|---:|
| media_enabled | false |
| media_voice_enabled | true |
| media_emoji_enabled | true |
| jev_media_enabled | true |
| media_min_chars | 10 |
| media_cooldown | 0s |
| media_candidate_limit | 5 |
| media_audio_score_threshold | 0.30 |
| media_emoji_score_threshold | 0.30 |
| media_tts_language | japanese |
| media_deepl_api_key | 空（未配置时直接使用会话模型） |
| media_send_text_with_audio | true |
| media_emoji_delay | 0.5s |

完整配置和字段说明以 `_conf_schema.json` 为准。

小说知识：`novel_context_enabled=true`、`novel_context_stage=integrity_knight`、`novel_context_dir` 留空使用内置资料；`novel_fact_limit=3`、`novel_dialogue_limit=3`、`novel_context_max_chars=6000`。关闭检索不取消角色核心记忆规则；配置修改需重载。

## 18. 测试与验证

必须使用 AstrBot 自带 Python 环境；系统默认 `E:\Python\python.exe` 没有 pytest。

从插件父目录运行：

```powershell
$env:PYTHONPATH='E:\AstrBot\backend\app'
& 'E:\AstrBot\backend\python\python.exe' -m unittest discover -s astrbot_plugin_alice_agent\tests -t . -v
```

快速模式：

```powershell
$env:PYTHONPATH='E:\AstrBot\backend\app'
& 'E:\AstrBot\backend\python\python.exe' -m unittest discover -s astrbot_plugin_alice_agent\tests -t . -q
```

编译检查：

```powershell
& 'E:\AstrBot\backend\python\python.exe' -m compileall -q astrbot_plugin_alice_agent
```

最后验证结果（2026-10-13，v0.9.0 小说资料接入）：

```text
Ran 257 tests
OK
```

2026-10-11 模型回退修复后的 225 项、2026-09-30 的 209 项、2026-09-23 的 109 项和 v0.7.0（2026-09-28）的 122 项只是历史快照。

覆盖范围：

- AstrBot Filter/Handler/stop_event 顺序
- 小说知识进入实际回复模型请求、与用户记忆分离、无新增模型调用
- 具体场景匹配、宽泛标签拒绝、童年/听闻/阶段边界、action与关系兼容、上下文预算、坏数据回退
- 旧默认画像内存迁移、每日事件的种子/模板/模型输出不伪造童年记忆恢复
- 当前 session Provider 调用及备用模型顺序回退、错误/空输出、取消与总超时边界
- Core/AstrBot/Jev infrastructure 边界
- Pipeline fallback
- Perception Parser 异常输入
- State/session isolation
- 真实临时 SQLite 会话恢复、时限／坏行过滤、首次恢复并发和取消写入清理
- 重建插件实例时恢复实际交付部分；实际分段发送取消后重载不恢复未发送后缀
- 每日事件关闭时仍可恢复心境；关闭会话连续性不读取既有历史快照
- Relationship/user isolation 和并发
- SQLite Memory 持久化与 scope
- Metadata/access tracking
- Importance/Reranking/relevance gate
- Memory → Planner → Response 数据流
- Event Bus 去重
- FakeClock 1h/5h/1d
- 连续 30 秒 Tick 下累计闲置、休息进入/恢复/退出、新消息唤醒、关闭休息驱动及效价向基线回归
- 临时真实 SQLite 指标恢复与追加、并发初始化、坏桶过滤、读失败保护、写失败重试、后台保存与卸载竞态、插件配置开关
- 默认关闭 TTS 私聊输入日志、显式开启仍记录实际参考与译文、诊断保留范围与关闭时不清理
- Agent Loop lifecycle
- 24h anti-spam budget
- pending action cancellation
- restart persistence
- Jev confidence/fallback/cache/schema/shadow
- Jev HTTP contract MockTransport
- 本地 VAD HTTP schema、范围/非有限值校验及归一化
- VAD/Jev/combined 三组分数、提示隔离、失败 fallback、缓存与本轮延迟
- 测试 JSON 开关、原子写入、关闭不写、敏感 Key 字段过滤
- Reactive/Proactive 分段保真、顺序、间隔、媒体一次性发送和部分失败
- Jev 不能绕过 cooldown
- 旧索引绝对路径迁移与素材存在性过滤
- 每日事件有界影响、回复上下文、LLM 无效文本回退、Jev 主用/影子/部分答案回退、跨重启幂等
- 本地媒体排序、Jev 抑制/候选选择及路径隐私
- Genie load/reference/tts 协议和 PCM→WAV 封装
- AstrBot Record + Plain 消息链

自动化测试不访问真实 Jev 网络 API，也不需要真实 Genie 服务。另有单独 localhost VAD smoke；本次未进行真实 Jev 准确率/成本评估或 QQ 消息验收。当前实例配置为 Jev `shadow` 且凭据已配置；不会在本地回归中调用或输出凭据。

## 19. 当前已知问题

1. 没有 TencentDB；如果需求仍要求 TencentDB，需要先确认是重新引入还是继续纯本地。
2. 当前实例有 Jev 凭据且处于 `shadow`，但缺少足以支持质量/成本结论的正式中文分层评估；不能据此宣称其已提高准确率或降低成本。
3. 官方说明 Jev 英语效果最佳；中文需要 shadow 数据集校准。
4. Jev raw HTTP 对 429/529 当前安全 fallback，没有有限重试/circuit breaker。
5. Jev Metrics 已可持久化，但尚未按模型/模式/日期分组，没有 p95 或端到端阶段耗时汇总；异常退出可能丢失未保存的计数。
6. 当前 confidence thresholds 全模块共享，未按任务分别校准。
7. Jev Perception topic 仍为 `general`。
8. Local Memory 是词法检索，不是真正的向量语义检索。
9. 心境与短期历史分别写入 SQLite，不具有跨文件原子提交；超龄历史只停止进入上下文，沉睡会话旧快照没有全局删除任务。历史恢复不能还原本版本启用之前未保存的内存内容。
10. ScheduledContextEvent 不会从自然语言自动提取。
11. SharedExperience 不会自动生成。
12. UserAvailability 没有桌面空闲、锁屏、游戏、睡眠等真实信号。
13. `qq_official` 主动发送能力取决于平台；aiocqhttp/NapCat 是主要目标。
14. 没有管理 UI 查看 Decision/Loop Metrics。
15. 没有端到端真实 QQ + Jev shadow 长期运行验证。
16. 旧核验曾发现 `127.0.0.1:8000` 不可连接；本次未重新探测外部服务，不能把旧结果视为当前状态。真实 Genie 音色、速度和 QQ Record 播放尚未验收。
17. Media 只覆盖 Reactive 回复；Proactive 内容仍为纯文本，但支持分段发送。
18. 新插件读取既有索引，但不包含 `/dg-build-audio` / `/dg-build-emoji` 等索引构建命令。
19. 小说检索是本地词法与具体短语匹配，未匹配表达可能漏召回；当前资料并非穷尽所有对白，也未经真实模型多轮角色验收。来源片段逐字核对不等于全部语义标注无误。

## 20. 明确未实现的功能

- TencentDB Agent Memory
- MemoryCore HTTP
- L0/L1/L2/L3 远端分层
- Autobiographical Memory 自动提取
- Emotional Memory consolidation
- 长期遗忘/合并/冲突事实版本管理
- Desktop Gateway
- WebSocket Desktop Client
- Embodied State
- Streaming Action Protocol
- Live2D
- LoRA
- 小说向量检索／全书问答（当前已有受限的本地角色资料与对白检索）
- 视觉/语音输入
- Tool Calling
- MCP Agent
- 多 Agent
- 复杂 Goal Planning
- 无限自主循环

不要把预留的数据结构描述成已经完成的功能。

## 21. 下一步建议与验证边界

优先顺序建议：

1. 先核对实际运行版本和当前配置，再做真实 QQ 文本、分段插话、重载恢复、语音播放与模型回退验收。历史实例已启用 Jev shadow，无需重复要求首次启用；shadow 的情绪混合仍生效，评估时必须控制这个变量。
2. 收集至少 100 条真实中文决策样本。
3. 人工标注 Perception/Planner/Proactive 正确答案。
4. 分模块计算 accuracy、calibration、fallback、disagreement 和实际延迟。
5. 按模块拆分 threshold，而不是继续使用全局 0.85／0.80；评估不同模块置信度是否可比较。
6. 根据真实错误率决定是否为 429/529 增加总超时受控的有限重试和 circuit breaker；重试也可能增加延迟与费用。
7. Decision Metrics 已在 v0.8.4 持久化，后续可补模型/模式分组、阶段耗时和分位数；不能把现有累计指标直接作为评估集。
8. 验证后再考虑先对 Planner 单独试用 hybrid；当前模式开关是全局的，按模块启用仍需补配置与测试。
9. 再决定是继续纯本地 Memory，还是重新实现 TencentDB Adapter。

如果下一阶段仍按此前路线，应先讨论而不是直接实现：

- Desktop Gateway
- WebSocket Protocol
- Embodied State
- Streaming Action Protocol

## 22. 新对话接手检查清单

新对话开始后先执行：

1. 阅读 `HANDOFF.md` 和 `README.md`。
2. 检查当前目录树和 `metadata.yaml`。
3. 检查最近文件改动，不假设本文档之后没有变化。
4. 运行当前全量回归测试；v0.9.0 于2026-10-13全量257项通过，v0.8.4的238项及更早结果为历史基线。
5. 检查 `alice_agent/` 是否仍无 AstrBot/HTTP infrastructure import。
6. 如果要改 AstrBot Adapter，重新核对当前安装版本和官方插件 API。
7. 如果要改 Jev Adapter，重新核对 TypeSafe 官方 API/OpenAPI 和 model list。
8. 如果要启用 Jev，先确认 API Key、隐私授权和 shadow 采样计划。
9. 如果要启用 Media，先停用旧 `deeplx_genie_tts` 的自动回复装饰，确认 Genie 服务、参考音频路径和平台 Record 支持。
10. 如果需求提到 TencentDB，先指出当前工作区没有该实现，再确认是否真的要重新接入。
11. 修改后重新运行全量测试，不只跑新增测试。

## 23. 可直接粘贴到新对话的启动提示

```text
继续开发现有 Alice Agent 项目。

项目路径：
C:\Users\Administrator\.astrbot\data\plugins\astrbot_plugin_alice_agent

请先完整阅读：
- HANDOFF.md
- README.md
- metadata.yaml
- _conf_schema.json
- 当前 tests

当前版本为 v0.9.1，含小说资料与每日主动概率，发布状态查看GitHub Releases。

重要事实：
- Memory 当前是纯本地 SQLite，不存在 TencentDB/MemoryCore Adapter。
- 默认扮演整合骑士爱丽丝，没有童年亲历记忆；小说资料作为独立character_knowledge上下文，宽泛tag不召回。内置228张剧情卡与28条精选对白，默认不检索后期资料，具体配置见docs/NOVEL_CONTEXT.md。
- Jev Decision Layer 已完成基础接入；schema 默认 jev_enabled=false/jev_mode=shadow。最近核对的实例显式启用且处于 shadow，不能把实例配置当作默认值。
- 首次启用 Jev 应使用 shadow，不得直接切 primary。
- ResponseGenerator 优先使用 AstrBot 当前会话 Provider，失败时按 `agent_runner.config.model.fallback_provider_ids` 顺序回退；共用原有每次调用的总超时预算。
- Reactive Media 已接入但 schema 默认 media_enabled=false；2026-09-29 核对的当前实例 `media_enabled=true`、`media_emoji_enabled=true`。主动文本消息默认分段发送。
- 本地语义 VAD 默认开启；Jev 可用时回复情绪始终混合 Jev 与本地 VAD，即使在 shadow；但 shadow 的分类、时机和媒体选择仍为原路径。时机模块在 Jev 可用时会产生观察请求，并受 3 秒首条硬期限约束。
- 逐轮诊断 JSON 默认关闭；启用会保存私聊文本及情绪/模型参数。
- TTS 私聊原文/译文日志默认关闭，需显式开启 media_log_tts_inputs；test_log_retention_days 默认 0 不清理旧诊断。
- Decision Metrics 默认持久化至 alice_metrics.db，每 30 秒及正常卸载保存，只有按任务类型累计的汇总，不包含私聊文本或用户标识。
- 持续 Tick 按最后一次用户交互计算闲置，补齐休息状态进入/退出；valence 在每日事件关闭时也逐步回归基线。
- 每日事件默认关闭；启用后按会话每天生成少量事件并持久化心境，可能增加 LLM/Jev 调用。
- 会话连续性默认开启：历史按 session 恢复最多最近 `history_limit` 轮，默认 8 轮／72 小时；关闭后不读写近期历史快照。心境即使每日事件关闭也可持久化。保存的是发送确认路径交给 AstrBot 的实际文本，包括主动消息；平台最终送达仍未知。
- alice_agent/ Core 不允许依赖 AstrBot 或 TypeSafe HTTP。
- v0.8.4 于 2026-10-11 全量 238 项、源码编译和配置 schema 解析通过；新增测试覆盖连续 Tick 的休息闭环、真实临时 SQLite 指标恢复、存储故障及卸载竞态、诊断保留和 TTS 日志开关。v0.8.3 的 209/225 项为历史基线；模型与平台仍使用 mock/fake，未重载生产实例。
- 历史媒体验证：此前本机 Genie 的角色加载、参考音频设置、TTS 均返回 200（TTS 返回非空原始 PCM），并完成一次实际 Renderer 合成 WAV→QQ 官方 Record→Silk 的本地贯通验证；翻译在该 smoke 中使用固定日文文本，未做真实 QQ API 送达测试。2026-09-29 历史配置为 `media_enabled=true`、`media_voice_enabled=true`、`media_emoji_enabled=true`、`test_log_enabled=true`；这些不是默认值，也不能视为当前已复核值。

开始修改前：
1. 审查当前真实代码，不根据旧对话猜测。
2. 运行全量测试并记录基线。
3. 指出需求中的错误前提和缺失信息。
4. 保持 Code > Model：State、Relationship、Drive、Memory score、Cooldown 和 Budget 继续由代码决定。
5. 外部服务失败不得破坏 Reactive Chat；Proactive 失败默认 DO_NOTHING。

本次任务：
[在这里填写下一阶段需求]
```

## 24. 交接结论

当前 Alice Agent 已具备完整的可测试骨架：

```text
Reactive Pipeline
+ Persistent local memory/relationship/autonomy
+ Event-driven Agent Loop
+ Conservative proactive behavior
+ Optional Jev bounded decision layer
+ Optional reactive TTS / emoji media output
```

后续重点是生产验证：每日事件的实际表现与费用、真实 QQ 长期运行、Jev 中文校准、真实 Genie/QQ 播放验收，以及指标分组与时间事件提取。休息状态规则、指标持久化和日志治理已在 v0.8.4 完成本地验证，仍需观察真实长期表现。后续开发必须继续保持 Core 与 AstrBot、Memory Backend、Decision Backend 和媒体 I/O 的可替换边界。

v0.9.0 本地新增角色知识与对白接入及提示词调整；后续需要真实模型角色验收，重点观察童年转述、默认阶段、具体场景命中、拒绝宽泛标签和与用户关系不混淆。v0.9.1包含这些资料和接入；旧v0.8.4不包含。更新后需要重载。
