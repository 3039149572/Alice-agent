# Alice Agent v0.8.3 — Conversation Continuity Across Reloads

Alice Agent 是与 AstrBot 解耦的、状态持续的事件驱动角色 Agent Core。AstrBot 仍只负责平台接入、当前会话 LLM Provider、消息发送和插件生命周期；事件、State、Drive、Relationship、Memory、Planner 与行为克制机制均位于独立 Core。v0.8.3 在 v0.8.2 基础上补齐插件重载后的短期对话与心境恢复。

> 当前工作区的内容记忆后端是纯本地 SQLite，不存在 TencentDB / MemoryCore Adapter。v0.6 没有伪造或重做远端集成。SharedExperience 已有模型和持久化入口，但尚未自动提取 Autobiographical Memory。

新对话或新开发者接手时，请先阅读 [`HANDOFF.md`](HANDOFF.md)。该文档集中记录当前真实能力、完整处理流程、测试基线、已知限制和接手检查清单。

源码仓库：[3039149572/Alice-agent](https://github.com/3039149572/Alice-agent)。服务器安装、迁移和后续更新步骤见 [`SERVER_UPDATE.md`](SERVER_UPDATE.md)。仓库只发布源码，音频、表情素材和实例数据独立保管。

2026-10-11 修复：模型调用失败、返回错误或空回复时，按 AstrBot 当前会话配置的备用模型列表顺序回退；保留原有总超时。新增 16 项回归测试，全量 225 项通过。重载插件后生效。

## 架构与数据流

```text
AstrBot / future Desktop Gateway
              ↓
          AgentEvent
              ↓
           EventBus
      ┌───────┴────────┐
      ↓                ↓
Reactive Pipeline    AgentLoop (cheap tick)
Daily Event / Mood    Daily Event once per local day
Jev/LLM Perception   TimeStateReducer
State Reducer        DriveReducer
Relationship         Scheduler due events
Memory Recall        CandidateAction
Jev Memory Gate      Cooldown / budget / score
Code Reranker        Jev Proactive Gate
Jev/LLM Planner      Code Policy Gate
ResponseGenerator      ↓ only when allowed
Media Local Rank    ProactivePlanner fallback
Jev Media Choice       ↓
Genie / Emoji       ResponseGenerator
      └───────────────┬┘
                      ↓
                AstrBot Adapter
```

`alice_agent/` 不导入 AstrBot。主动发送通过 `ProactiveSenderPort`，生产实现只存在于 `adapters/astrbot/proactive_sender.py`。

## 每日事件与心境

`daily_events_enabled` 默认关闭，以免未确认模型费用和角色行为前自动改变线上对话。开启后，每个会话按机器本地日期最多生成 `daily_event_max` 件小事件（默认上限 2，允许 0～3，部分日期为 0 件）。程序确定事件类别与来源，当前会话模型只负责按人物设定扩写 4～80 字的日常小事；不合规输出或模型失败会回退内置文本。下一天可有限概率延续前一天的小线索；前几天同会话的对话可作为“想起用户说过的话”的资料，但不会凭空断言用户后续状况。没有剑术或 TencentDB 等未在当前项目里确立的设定。

`persona_profile` 可配置角色姓名、明确虚构的生活背景、兴趣、作息和每日事件种子。该设定会影响日常事件骨架及回复/规划上下文；它不是用户事实，也不代表现实位置、日程或传感器信息。默认画像是普通、安静的日常生活，可按配置覆盖。

每日生活分享候选只在 `daily_events_enabled=true` 且已有可用事件时参与主动评估；默认允许时段为本机时间 09:00–21:00，每会话每天最多 2 次此类规划尝试。既有活跃会话保护、冷却、评分、Jev gate 和 `proactive_daily_budget` 仍生效，因此不会承诺或强制每天发送；每日事件总开关默认关闭。

### 回复时机与连续输入

`response_timing_enabled` 默认开启，确定性规则优先保护明确“不用回复”、明确“等我说完”、常见直接问句和求助；普通纯文本有 0.8 秒安静窗口用于合并连续补充，首条消息起最多等待 3 秒，分类耗时也计入硬上限。只合并可安全转成文本的普通消息，诊断保留每条原始消息边界；媒体或不确定组件跳过安静窗、单独处理（不保证零延迟）。新输入会先推进修订并使未交付旧生成失效，过期回复不会进入已发送历史；若分段发送期间插入新消息，仅记录已交付部分。等待与生成不持有共享发送锁，最终媒体/发送前会再次校验当前修订。

到达硬期限仍未完成的 Jev 分类会回退到本地规则；“不回复”不会被超时反转为回复。全局 Jev 关闭或不可用时只运行本地规则；`shadow` 只记录比较、不改变时机选择；只有 `hybrid/primary` 中达到对应 DecisionPolicy 置信度要求的模型结果才可能影响普通陈述的立即回复/等待/忽略，且不能覆盖明确用户指令或受保护的问句。关闭 `response_timing_enabled` 会恢复单条消息即时处理，不等待、不合并，也不调用时机分类。任何时机选择都不保证平台确认送达。

事件记录包括来源、类别、文本、话题线索、重要度、情绪变化、连续线索等，保存在 `alice_mood.db`，按会话隔离。Jev 可在当前心境下评估事件对 Alice 的 V/A/D、精力、压力和孤独感的主观变化；`shadow` 只观察，`hybrid/primary` 在数值置信度低于 0.80 时先尝试 LLM 评分并混合，LLM 无效则退回确定性规则，每轴仍受 ±0.12 的上限约束。心境每天以 0.75 惯性向角色基线回归，再叠加总幅度不超过 ±0.30 的事件影响；用户互动仍按原有 Reducer 渐进影响心境。今日经历和可选话题线索进入 Planner/Response/主动规划的状态上下文，不强迫每次提及。事件的 `memory_candidate` 目前只是记录字段，并未自动写入长期 Memory。

Reactive 与 AgentLoop 共享会话锁；同一天重复检查或插件重启不会重复施加事件影响。当天事件先存档，再一次性更新心境；如果两步之间中断，下次会复用存档补做更新。开启 LLM 扩写时每天每会话最多产生设定上限次数的模型调用，Jev 启用且可用时另有相同数量的评估调用。可分别关闭 `daily_event_llm_enabled`、`daily_event_jev_enabled` 只用本地规则。

## Jev Decision Layer

Jev 通过 Core 内的 `DecisionModelPort` 接入；TypeSafe HTTP 细节仅存在于 `adapters/jev/typesafe.py`。当前实现严格区分：

- Code：State/Relationship/Drive 数值更新、Memory 最终评分、Cooldown、预算和 Policy 覆盖。
- Jev：Perception 分类、Memory 二次相关性、Planner bounded choice、Proactive SEND/DEFER/DO_NOTHING gate、reactive media bounded choice，以及可选每日事件的有界情绪反应评估。
- LLM：低置信度或复杂场景升级，以及所有最终自然语言生成。

官方 HTTP 契约使用 `POST /v1/systemone`，Bearer authentication，请求只发送 `state`、`model`、`questions`；问题类型使用 `choice`、`noul`、`score` 和官方 `criteria` 字段。`GET /v1/models` 只作为模型发现接口。默认模型配置为可更新 alias `jev-latest`，不是在源码中假定某个永远有效的固定模型。

四种模式：

- `off`：不调用 Jev。
- `shadow`：分类与行为决策继续使用原有路径；用户明确要求的回复情绪是例外，Jev VAD 与本地 VAD 的混合会生效。
- `hybrid`：达到对应策略置信度要求时采用 Jev，否则升级原有路径。
- `primary`：达到当前策略置信度要求时采用 Jev；低置信度、失败或复杂推理仍升级原有路径。

Schema 默认是 `jev_enabled=false`、预选 `jev_mode=shadow`。最近核对的本机实例显式配置为 Jev enabled + `shadow`；这属于实例状态，不是插件默认值。`shadow` 的分类与行为决策仍选用原有路径，但回复情绪按本次用户要求混合 Jev 与本地 VAD；本次功能不会强制切换到 `primary`。启用后凭据由 AstrBot 密码配置或环境变量读取；源码和日志不记录密钥。无凭据时保持本地路径。

实际 Jev 决策采用下限为 0.80（`hybrid` 的高置信门槛默认 0.85）。感知、Planner 有既有 LLM 路径，可在低置信时回退；每日事件在非 shadow 模式下有有界 LLM 评分路径。Memory、Media、回复时机和 Proactive 没有独立 LLM 判断器，低置信时继续使用各自的本地／安全回退，而非声称已调用 LLM。不要把所有模块的 Jev 数值和置信度视作已校准的同一量尺。

各问题组有独立版本：`perception_v2`、`memory_relevance_v1`、`planner_v1`、`proactive_v1`、`media_v1`、`daily_event_v1`、`response_timing_v1`。短时缓存键包含 model、decision type、state hash、schema version 和问题 schema，避免跨场景复用。

### 用户情绪 VAD 融合与诊断

`vad_enabled` 默认开启；每条私聊用户消息会被发给配置的本地 VAD 服务（默认 `http://127.0.0.1:8001`，请求 `POST /vad`，超时默认 5 秒）。本地模型返回的三轴原始值按 1..5 保存，并用 `(分数 - 1) / 4` 映射为 0..1；Valence 最后转为 Core 的 -1..1，arousal 与 dominance 保持 0..1。服务超时、错误或返回值无效时，按可用的 Jev／LLM Perception 分数回退，不伪造本地 VAD 分数。

如果全局 Jev 与 Jev Perception 均启用，现有同一次 Jev Perception 请求也会给当前用户消息打 V/A/D 分数及情绪强度、温暖度、紧张度、压抑、情绪目标/变化和情感回应需要等语义标签。Jev 分数按请求定义的 0..4 归一化到 0..1；最近三轮上下文只提供给 Jev，本地 VAD 只评分当前消息，因此两者输入范围并不完全相同。最终回复情绪按轴混合 Jev 与本地 VAD，包括 `shadow`；当 Jev 整体或该轴 confidence 低于 0.80 时，先按置信度将该轴 Jev 与 LLM 数值混合，再与本地 VAD 混合。LLM 缺少有效数值时该轴仅用本地 VAD；Jev 不可用时也仅用本地 VAD。额外语义问题不增加 Jev 请求，但低置信度可能增加一次 LLM 感知调用。

逐轮诊断保存 `vad_model`、`jev`、`llm`、`jev_llm`、`combined` 分数（后两者为有效时才有），原始分数、Jev 结构化答案/概率/置信度与缓存信息，以及本地 VAD 和 Jev 延迟。`combined` 是实际选取的回复情绪分数，不再只是诊断。`api_latency_ms` 是 Jev 远端请求耗时（含网络往返）；缓存命中时另存当前查找耗时。`jev_vad_weight` 默认 0.5，即有效 Jev 一侧和本地 VAD 各半；Jev 低置信度时在 Jev 一侧按轴使用 `0.5 × confidence / 0.80` 权重先与 LLM 混合。此公式未经校准，不等于准确率提升结论。

### 测试记录与分段发送

`test_log_enabled` 默认关闭。启用后，每条完成或发送失败的私聊会在本地 `data/plugin_data/astrbot_plugin_alice_agent/test_turns/` 产生一个原子写入的 JSON，包括用户原文边界、时机选择/延迟/实际等待、生成/实际交给 AstrBot 的回复文本、有限长度的近期对话、本地 VAD／Jev／LLM／混合情绪分数（可用时）、Jev/VAD 请求诊断、Pipeline 状态/Planner/Memory/媒体摘要与发送结果。同轮感知或规划阶段同时运行 Jev/LLM 时，顶层 `decision_comparison` 保存双方决策与采用来源。记录可能包含私聊内容和会话/用户标识；请仅在受控环境开启并按隐私要求保管。不会保存配置对象或 API 密钥。关闭后停止新增文件，不删除旧记录；插件卸载/重载会使旧实例停止写入。

`segmented_send_enabled` 默认开启，`segmented_send_interval_seconds` 默认 1 秒。回复和主动消息按完整句子发送；无句末标点的长句和完整代码块不硬截断。`segmented_send_max_chars` 为兼容旧配置保留的软值。分段拼接与原文本一致。QQ 官方语音单独一条消息，其他平台语音只随第一段，表情只在最后一段之后发送；部分发送失败后不重发整条回复。Reactive 与 Proactive 对同一会话共用发送锁以维持顺序。若将 AstrBot 全局 segmented-reply 改为处理一般消息的模式，它可能再次拆分插件输出；此插件不会修改该全局设置。

### Jev 参与位置

1. Perception：bounded intent/emotion、8 个可并存 social signals、memory-worthy、五级 importance、relationship event 和 deep-reasoning gate。Jev 不修改 State 或 Relationship 数值。
2. Memory：一次请求并行判断所有候选的相关性；原 retrieval relevance gate 先执行，Jev 不能救回不相关 Memory；最终分数仍由代码计算。
3. Planner：只允许 `reply/acknowledge/comfort/clarify/tease/recall_then_reply/wait/ignore`，并给出 tone、length、directness、follow-up 和 memory choice。Jev 不生成自由文本 `response_goal`。
4. Proactive：仅在确定性预筛、Cooldown、活跃会话保护、daily budget 和 action score 已通过后调用。SEND 还需同时满足高置信度、自然理由、低打扰概率、相关上下文和 appropriateness；任何失败在主动路径中默认安静。
5. Media：语音候选按 Perception/State/ActionPlan 的 VAD、能量和文本相关性筛选；表情先以助手最终回复作语义资格筛选，再应用本地分数门槛。Jev 只收到筛后候选的 ID、描述、分数、VAD/能量及可选表情标签，不收到本地路径。`shadow` 保留本地选择，`hybrid/primary` 仅按统一置信度策略采用 Jev 结果。

`ActionPlan.response_goal` 暂时保留以兼容原有 LLM Planner 和 ContextBuilder，但 Jev 路径只写固定内部目标，不让 Jev生成自由文本。

### Decision Trace 与 Metrics

每次真实 Jev 请求产生独立 `decision_id`，记录 trace ID、decision type、schema version、model、mode、input hash、latency、结构化 answers/probabilities、confidence、fallback 和 token usage。默认不记录完整用户文本或 Memory 内容。

按 perception/memory/planner/proactive/media 分桶统计：calls、平均延迟、errors、timeouts、fallback 数与比率、high/medium/low confidence、LLM escalations、shadow comparisons/disagreements 与比率、LLM calls avoided、input/output tokens。缓存命中不伪装为远端 API 调用。

## TTS 与表情包

媒体功能只装饰 Reactive Pipeline 的最终回复，不改变回复文本、State、Relationship 或 Memory。默认 `media_enabled=false`，避免 Genie 未启动或旧 `deeplx_genie_tts` 插件仍启用时出现重复发送。

### 回复风格与表情语义对齐（v0.8.1）

Response Generator 仍只调用当前会话的 AstrBot LLM Provider 一次。调用前，代码会从当前消息组以及最近 16 轮中最多 4 条实际已发出的回复生成短提示：识别重复通用开场、连续问句，以及可能的详略要求或话题纠正。当前消息信号来自有限词语模式，可能误判叙述；提示要求模型先对照用户原话确认，才采用详略或纠正线索。它只提供写法建议，不改变 Planner 的 action，也不保证模型每次都遵守；复杂请求仍须保留必要解释、步骤和约束。这些提示由本地规则生成，不额外请求 LLM 或 Jev。

表情对齐使用最终回复中的少量明确语义线索，与索引素材的意图、标签、配文和描述作确定性匹配，并记录候选是否符合及简短原因。它不读取图片像素、不调用视觉模型，也不额外调用 LLM/Jev。没有合格候选时，选择器可以放弃表情；`abstained` 精确定义为语义合格数为零（筛选后、Top K 前），不等同于媒体开关、冷却、发送概率或平台发送结果。它是有限规则，不是通用中文理解，不能保证表情语义准确。最终是否发送仍受媒体配置和现有选择流程约束。

素材索引支持可选的 `intent`、`tags`、`caption` 字段；旧索引继续使用 `desc`，规则会识别有限的动作线索和已知引号配文。2026-09-29 核对的当前索引有 101 条旧格式记录，包含 `path`、`desc`、VAD 和能量字段，但没有显式 `intent`、`tags`、`caption`，所以未覆盖的词语或隐含语境可能被放弃或匹配不准。

启用 `test_log_enabled` 后，逐轮媒体诊断会记录候选 ID、意图类别、资格判断、规则原因和分数；逐候选明细最多 12 项，总候选数与语义合格数独立保留。诊断不复制私聊原文或素材路径。逐轮日志本身仍可能含原有用户/回复文本，默认关闭时不会新增记录，启用后应作为敏感数据保管。离线代码和测试不证明真实 QQ 送达或播放。

处理顺序：

1. 本地规则检查回复长度、概率和 session cooldown。
2. 从 `audio_index.json` / `emoji_index.json` 读取候选；兼容旧索引中的 Windows 绝对路径，并按配置素材目录重定位。
3. 使用 Alice 已有的 Perception、State 和 ActionPlan 计算 VAD/能量目标并排名音频候选。
4. 表情候选先按回复语义与索引标签筛选，再排名并取 Top K；`media_emoji_score_threshold` 是 Jev 也不能越过的本地硬门槛。
5. Jev 仍可按既有 bounded schema 判断 `send_voice`、`send_emoji`、`voice_choice`、`emoji_choice`，失败或低置信度退回代码策略。该改动不增加 Jev 请求；若原配置启用了 Jev media，既有请求会携带通过筛选的素材意图/标签/配文元数据。实例处于 Jev `shadow` 时，Jev 只记录比较，不替换代码选择。
6. 语音通过 Genie `/load_character`、`/set_reference_audio`、`/tts` 生成。目标为日文/英文时，如已配置 `media_deepl_api_key`（或 `DEEPL_API_KEY` 环境变量），先使用 DeepL 官方 API 翻译；无密钥、接口失败或结果无效时，回退当前会话 LLM Provider。目标为中文时不翻译。不使用旧插件的非官方免费接口。
7. QQ 官方语音独立发送、其他平台语音与首句同链发送，表情包默认延迟 0.5 秒单独发送；任一媒体失败都只记录告警，文本回复照常发送。

每次准备合成语音时，AstrBot 运行日志会出现 `Alice media: TTS inputs`，以 JSON 记录实际生效的参考音频路径、参考文本、处理后的回复原文及最终送入 Genie 的译文。该日志与 `test_log_enabled` 无关；可能包含私聊文本和本机路径，请按敏感日志管理。DeepL 密钥不会写入。

索引和素材配置留空时，会自动复用同级 `deeplx_genie_tts` 目录及其旧配置。客户端会把旧配置中的监听地址 `0.0.0.0` 规范化为 `127.0.0.1`。新插件不包含索引构建命令，素材更新仍需使用已有索引或外部预处理流程。

DeepL 官方 API 需要单独申请密钥；Free 密钥以 `:fx` 结尾，自动使用 `api-free.deepl.com`，其他密钥使用 `api.deepl.com`。翻译请求限时 8 秒，密钥不会写入日志。配置密钥后，待配音文本会发往 DeepL；实际用量和费用以 DeepL 账户为准。

## Event Bus

统一 `AgentEvent` 包含 `event_id`、`trace_id`、`timestamp`、`source`、`event_type`、`session_id`、`user_id`、`payload` 与 `metadata`。当前类型：

- `USER_MESSAGE`
- `TIME_TICK`
- `SESSION_STARTED` / `SESSION_ENDED`
- `USER_IDLE` / `USER_RETURNED`
- `MEMORY_EVENT` / `STATE_EVENT`
- `SYSTEM_START` / `SYSTEM_SHUTDOWN`

EventBus 使用有界 recent-ID cache 去重；重复用户事件不会再次进入状态更新和回复链路。事件模型接受新字符串类型，因此未来桌宠事件不要求改写 AgentLoop。

## Agent Loop 生命周期

AstrBot 的异步 `initialize()` 启动单一后台任务，`terminate()` 先停止并取消 Loop，再关闭 Memory、State、Relationship 和 LLM 资源。`start()` 幂等，插件 reload 不会在同一实例产生重复 Loop。

每个 Tick 只进行：

1. 清理过期调度与旧发送记录。
2. 产生 `TIME_TICK`。
3. 运行 Time State Update。
4. 更新三个 Drive。
5. 检查到期的 ScheduledContextEvent。
6. 生成 CandidateAction，并执行活跃窗口、Cooldown、预算与评分。
7. 只有分数通过才召回 Memory 并调用 Proactive Planner。

每日事件关闭时普通 Tick 不调用 LLM；启用后，每会话换日检查可能调用事件扩写 LLM 和 Jev。Loop 单次异常不会结束任务；连续异常使用指数退避，最高 300 秒。

## Time State 与 Drive

TimeStateReducer 让 `mood_arousal` 和 `energy` 以每小时固定上限回归 baseline，并将长时间未交互的 `chatting` 转为 `idle`。启用每日事件后，DailyMoodReducer 另外在换日时对 V/A/D、精力、压力和孤独感应用有惯性的恢复与小事件影响。所有值保持在定义范围内。

Drive 与 Emotion、Relationship 分开持久化：

- `social_need`：按小时饱和增长，增长率乘以 `0.55 + 0.25 * closeness`；休息时进一步降低。收到用户消息后减 `0.22`。
- `curiosity`：仅在存在未完成/定时上下文时按小时饱和增长；收到用户消息后减 `0.08`。
- `rest_need`：非休息时按 `0.35 + 0.65 * mood_arousal` 增长；休息时按恢复率下降。

概念上的饱和更新为：

```text
new = old + (1 - old) * configured_increment
```

增长率和三个 Drive 开关均可配置，程序端统一 clamp 到 `0..1`。

## Candidate Action 与 Action Scoring

第一版支持：

- `DO_NOTHING`
- `STATE_ONLY_UPDATE`
- `PROACTIVE_MESSAGE`
- `MEMORY_FOLLOW_UP`

每个行为都有 `action_id`、创建/过期时间、来源事件、状态（candidate/planned/executing/completed/cancelled/expired）和解释原因。

默认评分：

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

默认高门槛为 `0.72`，延后门槛为 `0.60`。权重全部在配置中。Relationship 只能小幅改变分数，不能绕过 Cooldown、相关性、用户活跃保护和预算。

没有明确上下文时，即使 social_need 很高，也只会得到低相关候选或 `DO_NOTHING`，不会调用 LLM。

## Proactive Planner 与 Response

规则评分通过后才调用独立 Proactive Planner。它只输出 `do_nothing` 或 `proactive_message` 的行为计划，不写最终文本。LLM 超时、JSON 错误或 Provider 失败都回退为 `DO_NOTHING`，绝不发送主动 fallback。

最终语言仍由现有 ResponseGenerator 生成，`ActionPlan.source="proactive"`。Memory 只是自然背景，禁止出现“数据库显示”“我检索到”等表达。

用户新消息会先增加 session revision 并取消 pending action。Planner 或 Response 结束后会再次验证 revision；过期主动行为不会发送。

## Cooldown 与 Anti-Spam

发送前依次检查：

- 全局最短间隔
- 单用户最短间隔
- 同用户同话题最短间隔
- 每用户滚动 24 小时预算
- 活跃对话保护窗口

默认值为 6 小时最短间隔、24 小时同话题间隔、滚动 24 小时最多 1 条、用户最后互动后 15 分钟内禁止主动插话。滚动预算比午夜重置更保守。

## Persistence

```text
data/plugin_data/astrbot_plugin_alice_agent/
├── alice_memory.db        # 本地内容记忆和原始轮次
├── alice_intelligence.db  # Memory Metadata、Relationship、SharedExperience
├── alice_autonomy.db      # Drive、session revision、schedule、cooldown sends、action status
├── alice_mood.db          # 会话连续性或每日事件启用时：心境和逐日事件
└── alice_sessions.db      # 会话连续性启用时：有界近期对话快照
```

重启后恢复 Drive、ScheduledContextEvent 和关键 Cooldown/预算记录。`session_persistence_enabled=true` 默认启用会话连续性：AgentState 保存于既有 `alice_mood.db`，近期对话保存在 `alice_sessions.db`，即使每日事件关闭也可恢复心境。首次处理某会话时读取近期历史，回复时机、感知、Planner、生成和每日事件使用同一会话的有效上下文；主动消息的已交付文本也会保存。不会导入旧 `test_turns` 或长期 Memory 的原始轮次。

近期对话受 `history_limit` 限制（默认 8 轮），恢复时只使用 `history_restore_max_age_hours` 内的内容（默认 72 小时，可配置 1–168）。每会话磁盘快照最多 64 轮，每轮用户与助手文本各最多 8000 字符；较大的自定义历史长度或单条超长文本会在重载后缩短。72 小时是上下文使用期限，沉睡会话的旧快照行没有全局清理任务，并不等于所有磁盘副本自动删除。

只有现有发送确认路径交给 AstrBot 的实际文本进入已回复历史；分段中断仅保存已发送部分，明确不回复的用户输入保留为 `assistant_text=null`。生成后尚未交付或已被新输入取消的回复不会存入快照。该确认仍不代表 QQ 平台送达回执。快照读写失败时继续使用内存历史，并在 AstrBot 日志报告错误类型；不会增加 Jev／LLM 调用。心境与历史分属不同写入，不保证突然断电时两者一起提交。

关闭 `session_persistence_enabled` 后不读取或新增对话快照，也不删除旧文件；心境只有在每日事件仍启用时才继续使用既有持久化路径。此开关和 `test_log_enabled` 独立：关闭诊断日志不会关闭会话连续性。会话快照包含私聊原文，按聊天记录管理。

## Concurrency

Reactive Pipeline 与 AgentLoop 共享原有 per-session lock；不同 session 不被全局锁串行。同一 session 的 State/Drive 时间更新和用户消息更新互斥。Relationship 保留 per-user lock。每日事件若启用，换日时的 LLM/Jev 调用会占用该会话锁；其他主动消息发送前仍通过 revision 做乐观有效性检查。

## Observability

INFO 仅记录 Loop start/stop、成功主动消息和重要错误。DEBUG 记录 Drive 值、Candidate、评分、阈值、Cooldown/预算阻断以及 `DO_NOTHING` 原因。

内部指标：

- `total_ticks`
- `candidate_actions`
- `proactive_planner_calls`
- `proactive_messages_sent`
- `do_nothing_count`
- `blocked_by_cooldown`
- `blocked_by_daily_budget`

## 配置

新增配置见 `_conf_schema.json`：

- `agent_loop_enabled` / `agent_tick_interval`
- 三个 `drive_*_enabled` 与每小时增长率
- `proactive_enabled`
- `proactive_score_threshold` / `proactive_defer_threshold`
- `proactive_min_interval` / `proactive_same_topic_interval`
- `proactive_daily_budget`
- `active_conversation_window`
- `planner_timeout`
- 七个主动评分权重
- `jev_enabled` / `jev_mode` / `jev_endpoint` / `jev_model`
- `jev_api_key` / `jev_timeout_ms`
- `jev_auto_threshold` / `jev_review_threshold`
- `session_persistence_enabled` / `history_restore_max_age_hours` / `history_limit`
- 四个 `jev_*_enabled` 模块开关
- `jev_media_enabled`
- `jev_memory_relevance_blend`
- `media_enabled` / `media_voice_enabled` / `media_emoji_enabled`
- `media_*_index_path` / `media_*_dir`
- `media_*_probability` / `media_*_score_threshold` / `media_cooldown`
- `media_genie_*` / `media_character_name` / `media_onnx_model_dir`
- `media_tts_language` / `media_deepl_api_key` / `media_default_ref_*` / `media_send_text_with_audio`

默认配置刻意偏保守。

## 测试

```powershell
$env:PYTHONPATH='E:\AstrBot\backend\app'
& 'E:\AstrBot\backend\python\python.exe' -m unittest discover -s astrbot_plugin_alice_agent\tests -t . -v
```

2026-09-30 v0.8.3 全量回归 209 项通过（`unittest discover`，AstrBot bundled Python）；源码编译检查和配置 schema 解析也通过。v0.8.2 为 195 项，v0.8.1 为 184 项，v0.8.0 为 155 项，v0.7.0 为 122 项。新增测试使用临时真实 SQLite 验证重建插件实例后恢复上下文与心境、取消分段发送只恢复已 yield 首句、隔离与期限、损坏数据修复、存储失败及并发首次恢复。Jev、LLM、AstrBot 与媒体使用 mock/fake，不调用真实模型或 QQ；这些测试证明本地控制流与框架交接，不证明线上聊天质量或平台实际送达。

## 当前未实现与限制

- 当前没有从自然语言自动提取 ScheduledContextEvent；只有结构、持久化与 `schedule()` 入口。
- 最近核对的实例已配置 Jev 且为 shadow，但这不证明其质量或成本收益；仍需在授权和隐私控制下积累分层中文样本并评估 0.85／0.80 与情绪混合权重等实验参数。
- Jev 官方说明以英文效果最佳、中文等 CJK 语言可用但效果并不完全等同；Alice 的中文真实对话必须用 shadow 数据验证，不能仅凭单元测试切 primary。
- Jev Perception 当前只提供通用 topic，占位为 `general`；复杂话题提取仍应升级原 Perception。
- raw HTTP Adapter 当前对 429/529 执行安全 fallback，没有在请求内部自动重试；若后续增加重试，必须限制次数并纳入总超时，尤其不能让主动路径长时间悬挂。
- UserAvailability 目前主要来自最近交互与手动状态字段；桌面空闲、锁屏、游戏和睡眠只是预留。
- 内容记忆是本地词法检索，不是真正的向量语义召回。
- `qq_official` 平台是否支持 AstrBot 主动 `Context.send_message()` 受平台能力限制；aiocqhttp/NapCat 是当前主要目标。
- 媒体功能当前只用于被动聊天回复；主动消息仍为纯文本。
- 2026-09-23 本机 Genie `127.0.0.1:8000` 的角色加载、参考音频设置、TTS 均返回 200，且一次实际 Renderer 合成的 WAV 成功通过 QQ 官方 Record→Silk 转换；该 smoke 使用固定日文文本，未测 DeepL/LLM 翻译。尚未验证真实 QQ API 送达/播放。2026-09-28 复核配置时 `media_enabled=true`、`media_voice_enabled=true`；新日志代码仍需插件重载后才能进入运行中的进程。
- SharedExperience 尚未自动提取；没有 Autobiographical/Emotional Memory consolidation。
- 未实现桌宠、WebSocket、Live2D、LoRA、小说 RAG、多 Agent、复杂 Goal Planning 或自主工具循环。
