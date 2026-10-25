# Alice Agent v0.9.3 交接文档

当前源码版本：`0.9.3`。本批恢复基线：Git `414faf5` / v0.9.2。此前0.10.0仅是未发布本地候选标记，已纳入0.9.2。GitHub发布状态以Release及其标签为准；服务器尚未由本次操作重载。最新验证与剩余任务必须查阅 [进度记录](docs/REPAIR_PROGRESS.md)，不要用metadata版本推断当前进程已加载。

用户已明确暂不进行真实测试；后续模型调用次数不设用户上限，但当前不因此启动任何请求。真实模型/QQ/Genie效果仍属未验收，保留后文计划供将来继续。2026-10-23新增当日主动过程文件及发布0.9.2的授权。

2026-10-24根据用户提供日志修复主动重试、每日事件生成时段和失败原因记录。技术失败释放候选但不退还调用计数，默认60秒起退避至900秒；SQLite保存新占位、所有者及重试时间，过期恢复与旧任务保护不依赖日志文本。旧版已尝试记录不能可靠区分技术失败与明确拒发，因此不自动清零或重启该事件。详见 [PROACTIVE_RETRIES](docs/PROACTIVE_RETRIES.md)。

本轮授权是依次修复审计项，排除关系与角色表现改进。关系Reducer、StateReducer、角色设定、小说内容和对白触发规则未改。关系存储增加的幂等标记只保护原有计数不因恢复重复累加；历史提示投影排除内部交付ID，不调整角色规则。

## 已实现与边界

| 部分 | 当前实现 | 证据边界 |
|---|---|---|
| 接入 | AstrBot私聊映射、去重、时机判断、消息合并、FIFO和新输入失效 | 模拟框架/平台；真实账号另验收。 |
| 模型 | 显式Provider回退、共享总预算、分Provider预算、取消传播、脱敏尝试计时 | 模拟Provider验证；不代表真实模型质量/供应商稳定性。 |
| 记忆 | 本地SQLite、中文倒排索引覆盖全历史、具体相关度门槛、元数据重排 | 没有TencentDB/远端MemoryCore；配置回退为in_memory时不持久化。 |
| 事实 | 有限自述提取、来源、当前/历史版本、纠错、重复合并、迟到保护 | 来源是用户陈述，不是真实性担保；旧原句不会自动重解释为事实。 |
| 数据治理 | 自己范围内查询/纠错/确认删除、固定状态保护、原句/版本/日志/快照保留 | 平台记录、外部日志、迁移备份及物理擦除不在命令保证内。 |
| 交付 | 实际文字/配音原文及部分前缀；被动/主动的本地提交意图和幂等重放 | 不重发，不证明QQ回执；发送与意图落盘之间不能伪造原子性。 |
| 媒体 | 决策、参考选择、翻译、Genie、文字优先、超时回退、文件租约 | HTTP模拟验证；音色/QQ Silk播放另验收。其他插件不受本插件TTS锁约束。 |
| Jev | 按任务模式/阈值、shadow观察与数值干预开关、分组指标、离线评估 | 默认数值干预false；待审核样例不能作为中文准确率/费用证明。 |
| 主动行为 | 原有时间、概率、预算、活跃/未回复保护、调度和规划；只读诊断 | 不保证每天发送；恢复保留冷却，真实权限/送达另验收。 |
| 角色知识 | 原有整合骑士边界、具体触发/阶段/动作过滤、静态资料 | 本轮不调整角色、关系或知识内容。 |

## 处理链路

私聊 → `adapters/astrbot/reactive.py` 登记revision/排队/合并 → Core生成 → 发送锁与用户数据gate → `reply_delivery.py` 文字/媒体交付 → `core/delivery_commit.py` 持久化实际交付意图 → 各库幂等提交 → 收据移除私聊文本。

主动Tick → 原有规则/概率/评分/规划 → 发送前重新核对 → 平台分段发送 → `core/proactive_delivery.py` 保存已确认前缀 → 原子更新自主库的冷却/每日送达/行动，再保存访问与历史。恢复只处理本地状态，不调用模型或平台。

数据命令 → 从当前事件取得身份 → 用户gate → 恢复未完成交付/清理 → scoped查询/纠错或删除预览 → 单次确认 → 记忆库原子删除与清理任务 → 元数据/快照/派生文字清理 → 旧待回复失效。

用户数据gate先于发送锁、会话锁；消息到达登记保持在这些锁之外，避免破坏合并和中断。生成器关闭必须逐层关闭子生成器，完成实际前缀提交后再释放持有权。恢复列表在取得锁后重新核对，防止已完成意图在用户删除后被旧快照重放。

## 模块定位

| 模块 | 责任 |
|---|---|
| `main.py` | AstrBot组合、配置、命令和生命周期。 |
| `adapters/astrbot/reactive.py` | 到达登记、时机/合并、revision与排队。 |
| `adapters/astrbot/reply_delivery.py` | 输出顺序、媒体等待/租约、前缀提交和诊断。 |
| `adapters/astrbot/send_coordinator.py` | 会话共享发送锁。 |
| `alice_agent/core/pipeline.py` | 独立生成流程、总预算、提交/恢复入口。 |
| `alice_agent/core/delivery_commit.py` / `proactive_delivery.py` | 被动/主动交付后的本地协调，不依赖网络/框架。 |
| `adapters/storage/delivery_journal.py` | 意图、完成收据；完成后不保留交付正文。 |
| `adapters/diagnostics/proactive_logger.py` | 固定 `proactive_today.log` 的UTF-8逐行JSON、同日追加、跨日原子替换和独立60秒换日检查。 |
| `alice_agent/proactive/attempts.py` | 事件占位、有限重试、退避和纯候选筛选；运行循环与只读诊断共用筛选规则。 |
| `adapters/memory/local_sqlite.py` / `fact_store.py` / `governance.py` | 内容、索引、事实版本与原子数据操作。 |
| `alice_agent/memory/data_service.py` | scoped命令、确认、用户gate、后台保留和恢复。 |
| `alice_agent/memory/metadata_store.py` | 重要度、固定/访问元数据、幂等访问计数。 |
| `alice_agent/decision/` | 分任务Jev策略、指标及离线评估。 |
| `scripts/verify_release.py` | 源码/默认值/边界/版本与安装包验证。 |
| `.github/workflows/checks.yml` / `release.yml` | Windows/Linux检查与依赖检查成功的发布流程。 |

## 实例数据

默认在AstrBot插件数据目录：`alice_memory.db`（内容、原始轮次、索引、事实、编辑、待清理任务），`alice_intelligence.db`（元数据、关系和各自幂等标记），`alice_mood.db`（状态/每日事件），`alice_sessions.db`（近期快照），`alice_autonomy.db`（Drives、活动、冷却、计划、每日概率/送达），`alice_metrics.db`（脱敏汇总），`alice_operations.db`（待完成交付意图/收据）。实际路径可被配置覆盖，不应拿生产数据作为测试夹具。

记忆schema v3迁移前使用SQLite备份API建立带时间和随机值的备份，失败不继续迁移；治理表是本批的附加表。原始轮次不被事实提取替代。禁止旧版直接读取新事实库；回退先停止插件并恢复对应迁移前副本，保留当前库以便恢复备份之后的新记录。

幂等标记/摘要收据随轮次数增长，不能直接按聊天期限删除，否则可能恢复重复累加。未完成意图含必要私聊文本，不能当普通日志裁剪。平台聊天、外部服务日志和备份须另行管理。

## 默认变化与使用

完整参数见 [README](README.md) 和 `_conf_schema.json`。生成60秒/单LLM链30秒/有备用时尝试15秒与预留5秒；媒体等待12秒、默认文字优先；默认启用保守事实提取；数据保留配置默认0；Jev数值情绪干预默认false。已有实例显式值不被文档更改。

`/alice-status` 不抽签、不主动发送；`/alice-data` 只管理调用者范围，不进入聊天流水。命令语义、保留任务、失败恢复和边界详见 [DATA_GOVERNANCE](docs/DATA_GOVERNANCE.md)。恢复任务不依赖主动循环或日志开关。

## 验证和下一步

测试必须使用AstrBot对应环境。本机为 `E:\AstrBot\backend\python\python.exe`，框架路径 `E:\AstrBot\backend\app`。使用临时真实SQLite测试跨库失败/重启，Provider/平台/TTS使用模拟；从最终ZIP解包后运行完整测试，不能只测工作树。

CI固定官方AstrBot 4.28.1源码，检查全部实际schema默认值、Core边界、源码编译和版本，然后测试Windows/Linux解包产物。发布job依赖这些检查，远端是否实际通过必须另看Actions证据。[RELEASE_CHECKS](docs/RELEASE_CHECKS.md) 说明构建与门禁。

剩余真实验收：审核中文决策标签并用选定模型生成预测；验证主/备用超时预算、中文决策质量/费用、Genie音色与真实QQ送达/播放、重载后状态。必须先明确测试实例与接收账号，不能把mock或离线评估工具作为这些效果的证明。

历史架构/路线图保存在 [v0.9.1交接](docs/HANDOFF_V0_9_1.md)，不是本轮完成状态；本轮审计项与未完成项以 [REPAIR_PROGRESS](docs/REPAIR_PROGRESS.md) 为准。未授权的新远端后端、角色/关系策略及自动自传式提取不因历史路线图被视为已实现或本轮新增范围。
