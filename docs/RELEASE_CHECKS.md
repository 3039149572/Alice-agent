# 发布检查

当前源码版本为0.9.4，新增未回复后的有限等待及滚动次数限制；0.9.3的技术重试、每日事件时段及失败诊断继续保留。0.9.2已合并此前0.10.0本地修复候选。版本号不等于远端发布状态。
本地完整解包验证及当日主动诊断覆盖见 `VALIDATION_CURRENT.md`，
远端发布结果须以对应GitHub标签、Release与Actions实际结果为准。

`scripts/verify_release.py` 检查源码编译、配置schema/实际默认值、Core依赖边界、版本一致性，并生成带逐文件SHA-256清单的源码ZIP。只打包源码、文档、测试、脚本和静态知识；不打包实例数据库、配置、密钥文件、媒体库或缓存。静态 `alice_agent/knowledge/data/` 单独保留，不能被运行数据排除规则误删。

本机使用AstrBot自带Python与框架路径，构建后从ZIP解包目录运行完整测试。通过工作树测试不能替代对解包文件的检查。SHA-256用于核对内容，不是外部数字签名。

CI使用Python 3.12，在Windows和Linux上测试解包后的源码。框架源码固定为官方 [AstrBot v4.28.1](https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.28.1) 的 [ab42c0d9b726d82ad0f9563e04c53a4460c00d61](https://github.com/AstrBotDevs/AstrBot/commit/ab42c0d9b726d82ad0f9563e04c53a4460c00d61)。Python依赖取自该提交的pyproject，不构建框架前端；依赖自身仍按上游声明解析，因此不是完整依赖锁定环境。首次安装可能较慢。

主分支发布工作流必须等待整个检查工作流通过，再取Linux检查产物创建Release；版本已存在时不覆盖已有Release。分支和PR也运行检查。这里的工作流门禁不等于已配置GitHub分支保护或禁止管理员手动发布。

修改工作流并不代表远端CI已经运行。当前是否提交、推送、发版、重载生产、真实模型/QQ验收，分别记录在 `REPAIR_PROGRESS.md`；只有对应直接证据出现后才能标为完成。
