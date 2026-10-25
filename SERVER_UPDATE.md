# 服务器安装与更新

## v0.9.3 更新

安装 [v0.9.3 Release](https://github.com/3039149572/Alice-agent/releases/tag/v0.9.3) 的 `alice-agent-source.zip`，
或通过仓库更新/重新安装，再重载插件。先备份配置及整套插件数据。
本版保留既有额度和抽签，新增加的技术失败重试默认60秒起退避、最长900秒，仍受当日尝试上限限制。
每日事件不再在允许时段外新调用模型；已有缓存可读。日志细分失败类型、占位及重试时间，
重载后从 `proactive_today.log` 的startup/heartbeat核对 `plugin_version=0.9.3`。

旧版本当天“已尝试”记录缺少失败类型，升级不会清零或自动重试这些旧事件，下一日的新记录采用新机制。
不要为重试删除数据库；详见 [主动重试与时段](docs/PROACTIVE_RETRIES.md)。
发布GitHub不代表服务器已经更新/重载，真实模型/QQ验收仍需单独完成。

## v0.9.2 更新

从 [v0.9.2 Release](https://github.com/3039149572/Alice-agent/releases/tag/v0.9.2) 下载验证后的 `alice-agent-source.zip`，或通过本仓库在AstrBot中更新/重新安装，然后重载插件。本版合并此前未发布、曾标记为0.10.0的本地修复；0.10.0从未作为远端Release发布。当前范围和验证结果见 `docs/REPAIR_PROGRESS.md`。

重载后查看插件数据目录的 `proactive_today.log`，通常为 `data/plugin_data/astrbot_plugin_alice_agent/proactive_today.log`。同日追加、重启保留；按服务器本地日期跨日覆盖，插件运行期间最迟下一次60秒检查换日。无需开启聊天诊断开关。具体阶段、原因和时区见 [诊断文件说明](docs/PROACTIVE_LOG.md)。发布GitHub并不代表服务器已经更新/重载。

本版声明AstrBot `>=4.28.1,<5`。正式升级前停止插件，备份实例配置和整个插件数据目录（包含新 `alice_operations.db`、全部SQLite数据库及相关WAL/SHM文件）；素材另行保留。已有记忆库迁移前自动保存SQLite备份，备份失败不继续迁移。先在测试实例安装验证包，重载后检查实际源码/版本，再做真实模型、Genie与QQ验收。

新保留配置默认0，不主动清空旧记忆。默认文字先发、媒体上限12秒、生成预算60秒，Jev数值情绪干预默认false；已有显式配置优先。用户命令与保留范围见 `docs/DATA_GOVERNANCE.md`。

回退不能用旧检索逻辑直接打开新事实库。停止插件后恢复对应迁移前备份，并保留当前整套实例数据供恢复备份之后的新记录；未完成交付意图不能当作普通日志删除。没有生产升级前，不执行这些替换操作。

## 以下为v0.9.1历史安装参考

源码仓库：https://github.com/3039149572/Alice-agent

v0.9.1 包含主动分享链路修复、心情/每日事件驱动的当天概率，以及此前的整合骑士角色知识与精选对白。源码、配置字段定义、提示词、测试和 `alice_agent/knowledge/data/` 静态资料随版本发布；聊天记录、密钥、实例配置、音频和表情素材独立保管。角色知识配置见 `docs/NOVEL_CONTEXT.md`。

## Release 安装与主动消息设置

[下载 v0.9.1 Release](https://github.com/3039149572/Alice-agent/releases/tag/v0.9.1) 中的 Source code (zip)，可用于AstrBot本地ZIP安装；也可通过下方仓库地址更新/重新安装。升级前备份实例配置与 `data/plugin_data/astrbot_plugin_alice_agent`，不要删除数据库或加载两份插件；完成后重载。

旧配置不会因为新默认值而自动覆盖，请核对：

```json
{
  "agent_loop_enabled": true,
  "proactive_enabled": true,
  "daily_events_enabled": true,
  "daily_event_max": 2,
  "proactive_daily_score_threshold": 0.35,
  "proactive_daily_probability": 0.55,
  "proactive_daily_probability_llm_enabled": true,
  "proactive_daily_budget": 1,
  "proactive_daily_attempt_limit": 2
}
```

这些是合并到原配置的字段，不是整份配置。不要覆盖模型、平台或密钥配置。开启每日事件会有扩写调用；新增分享意愿模型每会话每天至多1次，失败回退规则。关闭 `proactive_daily_probability_llm_enabled` 可只用规则。

先发一条私聊登记会话；15分钟活跃保护结束后，在服务器本地09:00–21:00参与判断。确认进程/容器时区（中国时间可设 `TZ=Asia/Shanghai`）。看 `PROACTIVE startup`、`PROACTIVE session=... decision=...` 日志定位评分、抽签、未回复、冷却或预算阻断；同原因每会话每小时最多记录一次。抽签正反结果会保持到次日，不要为重抽而删除数据库。具体公式、调用成本和平台差异见 [主动消息说明](docs/PROACTIVE.md)。

仓库工作流在main的版本文件更新时自动创建对应标签与Release；已有同名Release不会覆盖。应核对Release实际存在，工作流成功不代替QQ发送验收。

## 推荐：使用 AstrBot 插件管理

在服务器的 AstrBot 插件管理中，通过仓库地址安装：

```text
https://github.com/3039149572/Alice-agent
```

更新前备份 AstrBot 数据目录中的插件配置和 `plugin_data/astrbot_plugin_alice_agent/`，保留当前可用版本。后续在插件管理中更新 Alice Agent，完成后重载插件；如果后台提示需要重启，则重启 AstrBot。

安装目录名称以服务器实际安装结果为准，不要在保留旧 Alice 的同时重复安装第二份。服务器已存在手工复制的插件目录时，先按下一节备份和迁移，避免加载两份插件。

## 已有目录的首次迁移

1. 在 AstrBot 中停止 Alice Agent，备份现有插件目录、配置和持久化数据。
2. 将旧插件目录移到 AstrBot 插件扫描目录之外留作回滚，再通过仓库地址安装。
3. 保留并核对原插件配置；数据库仍保存在 AstrBot 的插件数据目录中，不放进 Git 仓库。
4. 核对媒体、VAD、Genie 等服务地址和路径，然后启用插件。

Windows 本机路径不能直接复制给 Linux 服务器。跨主机迁移音频、表情素材后，单独设置 `media_audio_index_path`、`media_emoji_index_path`、`media_audio_dir`、`media_emoji_dir` 和默认参考音频路径。若服务器使用 Docker，路径和服务地址需要从 AstrBot 容器内可访问；容器内的 `127.0.0.1` 指向容器自身。

## 手动 Git 更新

仅当插件目录本身是本仓库的 Git 克隆时，在该目录执行：

```bash
git status --short
git pull --ff-only origin main
```

然后重载 Alice Agent。若存在本地源码修改或分支分叉，先备份并处理，不要用强制重置覆盖修改。普通更新不会主动清理未跟踪的素材，但插件管理器是否保留插件目录中的自定义文件取决于更新实现，因此建议将素材放在独立持久化目录中。

## 更新后检查

- 在日志中确认 Alice Agent 加载成功，并测试一条私聊。
- 核对备用模型配置属于该会话使用的 AstrBot 配置，配置项为 `agent_runner.config.model.fallback_provider_ids`。
- 模型失败时日志会出现 `Alice LLM provider ... failed (...); trying fallback provider ...`。总超时仍由 `llm_timeout` 限制；若主模型耗尽总时限，就不会继续尝试备用模型。
- 如果启用媒体，另外检查语音、表情素材和服务器上的外部服务。
- v0.8.4 默认开启汇总指标持久化，新增 `alice_metrics.db`，也应纳入实例数据备份；关闭 `decision_metrics_persistence_enabled` 不读写或删除旧指标。
- TTS 原文、译文和参考路径日志从 v0.8.4 起默认关闭；排障时显式开启 `media_log_tts_inputs` 并重载。`test_log_retention_days` 默认 0，不自动删除诊断；设为正数才在新增诊断时按修改时间清理过期文件。

2026-10-11 v0.8.4 本地全量 238 项测试、源码编译及配置 schema 解析通过。持久化验证使用临时真实 SQLite，模型和平台使用模拟接口；这不等于服务器或 QQ 平台已完成验收。
