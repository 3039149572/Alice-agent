# 服务器安装与更新

源码仓库：https://github.com/3039149572/Alice-agent

当前发布版本为 v0.8.4。本仓库包含插件源码、配置字段定义、提示词和测试，包含 AstrBot 备用模型回退、休息状态闭环及决策指标持久化。聊天记录、密钥、实例配置、音频和表情素材不随源码发布。

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
