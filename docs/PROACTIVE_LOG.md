# 当日主动消息诊断文件（v0.9.3）

固定路径是AstrBot插件数据目录中的 `data/plugin_data/astrbot_plugin_alice_agent/proactive_today.log`。
以实际 `StarTools.get_data_dir("astrbot_plugin_alice_agent")` 返回的目录为准，容器内路径需映射到宿主机才能直接查看。
文件在插件初始化时自动建立，无需开启 `test_log_enabled`、debug或主动发送，与逐轮聊天诊断文件独立。

## 每日覆盖规则

- UTF-8编码，每行一条完整JSON，包含中文 `message` 和 `reason`，可用文本编辑器查看。
- 同一服务器本地日期内持续追加；同日重载或重启读取首行日期，保留已有记录。
- 本地日期改变后原子替换旧内容，文件名保持 `proactive_today.log`，不生成按日归档。
- 插件运行时有独立的60秒检查，即使后台循环关闭或没有会话也会换日；启动或主动记录也立即检查日期。
- 停机期间不执行覆盖，下次启动再检查。文件占用、磁盘只读/写满等失败限频报告并后续重试，磁盘异常时不能保证完整记录。
- 日期按运行进程/容器本地时区划分，首行记录时区和UTC偏移。希望按中国自然日统计时，将服务器/容器时区设为 `Asia/Shanghai`，重载后核对首行与 `startup` 时间。

## 查看未发送原因

先检查首行 `date`，再按 `session_id` 筛选。每轮有 `tick_id` 便于串联；平台分段记录只有 `session_id`，按相邻时间对应到同一发送过程。

v0.9.3的startup/heartbeat增加实际metadata版本、退避参数，以及“本地自然日尝试”与
“滚动24小时发送预算”的周期说明。`daily_events.source` 区分cache/generated/deferred/disabled，
`cache_hit=true`及 `generation_model_attempts=0` 表示本次复用缓存，没有再次生成事件。
该attempts字段是模型客户端调用尝试数，Jev自己的cache命中仍以各次结果的cached字段区分，不能当成供应商账单计费数。

| 阶段 | 含义 |
|---|---|
| `startup` / `heartbeat` / `loop_disabled` | 开关、时段、间隔、评分、预算及尝试上限；心跳约每小时一次。 |
| `tick` | 登记会话数和到期待办数；无会话时不会产生主动候选。 |
| `evaluation` / `daily_events` / `candidate_availability` | 此会话被评估、事件数量、超过七天未活跃、事件关闭或无可用事件。 |
| `blocked` | 活跃保护、时段、未回复、无候选、冷却、预算、评分、概率安静、尝试耗尽、模型失败/安静、正文为空或发送拒绝。每次判断均记录，不沿用INFO日志每小时去重。 |
| `score` / `probability` / `attempt` | 评分与门槛、当日概率/随机数/允许结果/来源、尝试及已送事件数量；不额外抽签。 |
| `jev` / `planner` / `generation` | 动作、简短原因、正文字符数；`*_start` 后缺少完成记录可定位中断阶段。 |
| `daily_events_deferred` / `daily_event_model_start` / `daily_event_model_result` | 未到生成时段、实际准备调用的任务以及ok/error/skipped/template_fallback结果；区分缓存检查与模型调用。 |
| `attempt_claimed` / `attempt_finalized` / `attempt_recovered` / `retry_scheduled` | 占位、明确决策结束、过期恢复和退避；含事件ID、所有者/关联trace、剩余等待及下一次时间。调用次数不退还。 |
| `pre_send_blocked` | 新消息、候选过期、生成跨日/离开发送时段或发送前冷却复检不通过。 |
| `platform_rejected` / `platform_error` | 框架返回False或异常类型，再结合适配器运行日志排查路由、权限及平台限制。 |
| `platform_accepted` / `send_result` | 框架接受段数，`accepted`、`partial` 或 `not_accepted`，不等同QQ送达/已读。 |
| `delivery_commit` / `delivery_pending` | 本地提交完成或待恢复，恢复不会重发。 |
| `cancelled` / `evaluation_error` / `loop_error` | 取消或异常类型及中断阶段，不视为成功发送。 |

`blocked.reason_code`分别表示daily_events_empty、daily_events_attempted、daily_events_delivered、
attempt_limit、daily_budget、retry_wait、attempt_in_progress和attempt_ownership_lost，
不再将“没有可用事件”和“额度用完”合并成同一句。
`jev`同时记录mode、status、cached、error_kind、failure_reason、http_status和trace_id：
timeout为超时，authentication为鉴权/权限，rate_limit为限流，upstream_error为上游错误，
connection_error为传输失败，invalid_response为返回/答案校验失败。
这里只保存有限错误分类和HTTP状态，不记录完整异常正文、响应或请求密钥。

## 数据及成本边界

文件含会话/用户标识和简短规则/模型原因；模型原因可能提及上下文，应按诊断资料保管。
不记录完整聊天、生成正文、提示词或配置密钥，异常只记录类型。
`/alice-data` 清理相关上下文时也清除可归属的日志行；其他用户与全局心跳保留。

记录现有判断不会触发额外LLM、Jev、抽签或主动消息，也不修改规则和额度。
单日大小随会话数和Tick次数增长，换日覆盖；AstrBot运行日志和 `test_turns` 不受此覆盖规则影响。
突然进程退出/断电可能损失最后一条正在追加的记录；本文件不是平台回执或恢复数据库。
发布和安装本版不会自动重载服务器，更新后重载插件才开始记录新流程。
