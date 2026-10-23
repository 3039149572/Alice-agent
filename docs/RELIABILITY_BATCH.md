# 2026-10-21 本地修复批次（未发布）

> 本文件记录上一次暂停时的历史批次。2026-10-22已按继续指令恢复，当前本地版本为0.10.0；数据治理/被动与主动交付恢复、入口拆分和发布检查已接入。当前状态以 `REPAIR_PROGRESS.md`、根目录 `HANDOFF.md` 为准。

本批在 v0.9.1 / `417fc1b` 基础上修改，尚未提交或推送、未发版、未重载生产插件。metadata 版本仍为 0.9.1，不应仅凭版本号判断进程是否包含本批源码。关系规则、角色设定、小说资料和对白触发规则未改。

## 已接入的修复

- 通用主题不再为无关记忆提供相关度。中文倒排索引覆盖全部用户／会话历史，原 `local_memory_candidate_limit` 现表示单批读取数量，不再限定最近300条。旧库补建索引，更新内容同步更新索引。
- 同一事件循环、同一 Genie 地址和角色的参考音频设置与合成互斥，翻译可并行。生成文件独立持有；交给框架后保留至少5分钟再回收，初始化只清理超过24小时的旧文件。其他插件对 Genie 的调用不受本插件锁控制；仍应避免同时启用旧TTS插件。
- 历史提交使用实际配音的原文和实际发送的文字，不再把完整生成文本当成完整配音。分段中断或关闭生成器只提交已交付部分。
- `turn_timeout=60` 为生成计算总预算，`llm_timeout=30` 仍为单次模型链的共享总时限；LLM请求共享剩余预算并记录阶段耗时。发送锁须维持会话顺序，逐句间隔不属于模型计算预算。
- 有备用模型时，`llm_provider_attempt_timeout=15` 与 `llm_fallback_reserve_seconds=5` 在原总时限内分配尝试时间；取消不触发下一模型。没有备用模型时保持全部原预算。Provider诊断仅保留最近256次耗时与状态，不含提示词、回复或用户标识。
- `media_text_first=true` 在同时发送文字与语音时先发文字，再补独立语音；false保留旧顺序。`media_render_timeout_seconds=12` 限制初始化和生成过程，包括翻译、锁等待和Genie请求。仅语音模式保持显式配置。
- `memory_fact_extraction_enabled=true` 从明确的当前用户自述提取有限类型事实，保留逐条来源，重复陈述合并来源，新陈述产生替代版本，迟到旧陈述不覆盖当前事实。事务失败回滚同次原始轮次和全部事实。存储为用户陈述，不保证现实真实性；疑问、引述、假设、历史、未来计划和角色扮演不自动变成当前事实。
- 私聊命令 `/alice-status` 查看本会话主动候选、评分、已保存抽签、拦截原因和已知时间门槛。不登记新会话、不生成事件、不调用模型、不重抽、不执行主动发送；时间门槛通过仍不等于保证发送。
- `jev_task_policies` 可分别覆盖 perception、memory、planner、proactive、media、daily_event、response_timing 的 mode、auto_threshold、review_threshold。未指定继承全局，全局 `jev_enabled=false` 仍阻止全部远端请求；采用下限仍为0.80。
- **默认 `jev_affect_intervention_enabled=false`**：生产 shadow 路径保留Jev数值观察，但不混入最终情绪。设为true可恢复旧版要求的Jev／本地VAD混合；这不改变关系Reducer或角色设定。
- Jev汇总指标新增UTC日期、模型、模式、任务分组，最多256组，保留计数、用量、固定桶延迟直方图和p95上界估计；缓存不计为远端请求。旧指标可恢复，坏组跳过。

## 数据与恢复

升级已有记忆库到本批schema v3前，使用SQLite备份API在同目录保留 `*.pre-v3-时间-随机值.bak`；备份失败就不继续迁移。备份包含私聊数据，应按原数据库权限管理。原始轮次保留；旧原句记忆不会被批量重解释为经过审核的事实。

若回退到旧源码，应先停止插件，并使用对应迁移前备份恢复记忆库；旧源码不认识事实版本状态，不能直接用旧版检索逻辑读取新事实库。备份之后产生的新数据不会自动回到旧库，应另行保留当前库供恢复。

## 中文评估工具与验收边界

`scripts/evaluate_decisions.py` 只计算已经保存的预测与标签之间的结果，不调用模型。标签／预测均为JSONL，以唯一id及task对应；预测中的answers使用实际schema字段名，另可记录confidence、latency_ms、fallback。标签包含expected、stratum、reviewed。

默认只计算 `reviewed=true` 的标签。`docs/decision_eval_zh.proposed.jsonl` 是10条待审核样例，全部为false；不是正式中文评估集。`--include-proposed` 仅用于格式检查。报告包含覆盖率、缺失数、分任务／分层／字段一致率、置信度分桶、ECE、回退率及p95；不含原始聊天文本，也不自行声称模型质量已经改善。

示例：

```powershell
& 'E:\AstrBot\backend\python\python.exe' scripts/evaluate_decisions.py --labels reviewed_labels.jsonl --predictions saved_predictions.jsonl --output report.json
```

本地验证使用模拟Provider、MockTransport、临时SQLite；真实模型准确率、费用、真实Genie音色和QQ送达／播放仍未验收。

本批最终回归：**294项通过（新增25项），178个Python源文件编译、配置schema及实际默认值、评估CLI和Git差异检查通过**。未修改关系规则、角色设定、小说资料或提示词。详细记录见 `REPAIR_PROGRESS.md`。

## 本次暂停点

用户要求完成当前批次后停止。数据查询／纠错／删除命令、保留期限、跨数据库交付恢复、入口拆分和发布CI门禁尚未开始；继续时从 `REPAIR_PROGRESS.md` 的R10开始，R07的数据保留部分也依赖R10。完整发版及真实平台验收需另行继续。
