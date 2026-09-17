# video-extract

video-extract 是 VideoLearning 工作区的确定性媒体、来源包和笔记工具。运行时通过 WorkspaceConfig 定位代码、Media 和资料库；机器特定路径只保存在 workspace.toml 或 locator 中。

~~~text
video-extract workspace show --json
video-extract workspace doctor --json
~~~

## 媒体提取

plan 只读，ensure 把请求的已有媒体放进 canonical package。语言参数只选择来源已有轨道，不触发翻译或合成。

~~~text
video-extract plan SOURCE --media all --language original --workspace /path/to/workspace.toml --json
video-extract ensure SOURCE --media all --language original --workspace /path/to/workspace.toml --json
~~~

请求的语言轨不存在时会得到结构化 unavailable。平台登录、验证码、DRM 或付费墙仍需要用户处理。

## 本地来源和来源笔记

本地音视频、SRT、Markdown 和 UTF-8 文本先导入工作区：

~~~text
video-extract source import LOCAL_INPUT --workspace /path/to/workspace.toml --json
~~~

笔记流程只消费已管理的本地 package。prepare 会复用正式字幕；缺少转写时沿用本项目现有 faster-whisper 实现。语义步骤返回 awaiting_ai 和恢复 argv。

~~~text
video-extract notes prepare PACKAGE --workspace /path/to/workspace.toml --json
video-extract notes finalize PACKAGE --workspace /path/to/workspace.toml --json
~~~

finalize 验证笔记、导出到配置的资料库并更新索引。视频笔记使用真实视觉证据；音频/SRT 使用时间戳；文档使用标题或段落定位。

## 中文听觉版

中文听觉版由 mandarin-audio skill 消费同一个 package。它优先规范化原生中文轨，否则只对确认的英文来源调用配置的 pyVideoTrans podcast。规范结果位于：

~~~text
<PACKAGE>/listening/zh-CN/podcast.zh-CN.mp3
~~~

这是自然节奏听觉版，不是同步配音。pyVideoTrans 的私有恢复资料由其自身管理。

## 状态、验证与兼容

Media package 是事实来源；资料库导出、SQLite 索引和播放视图是可重建派生物。现有 schema 1–5 package 继续由 status、verify 和 validator 读取，不进行批量自动迁移。

~~~text
video-extract status PACKAGE --json
video-extract verify PACKAGE --goal notes_zh --json
video-extract library status --json
~~~

旧的 goal-driven planner 保留为内部兼容实现，不再是 plan/ensure 的公开入口。低层 transcribe、prepare-evidence、scan、acquire 和平台命令继续服务已有工具与维护任务；新 agent 使用上面的正式入口。

## 安装

~~~text
brew install uv ffmpeg
uv sync
uv run playwright install chromium
uv tool install --force --editable /path/to/video-extract-core
video-extract install plan --json
video-extract install apply --json
video-extract install check --json
~~~

`integrations/skills/`、`.codex/agents/` 和 Python 工具都只在本工程维护。`install apply` 在链接宿主入口前备份已有副本；`install check` 报告源码修订、内容指纹、依赖、安装漂移和确切维护位置。不要直接修改链接目标之外的安装副本。

稳定能力入口由工程内唯一映射维护，调用方只保存能力 ID 和公开契约版本：

~~~text
video-extract capability list --json
video-extract capability check source.notes --json
video-extract capability run source.notes --request request.json --json
~~~

`source.notes` 的请求使用 `contract_version: 1`，包含 `action`（`prepare` 或 `finalize`）、`workspace`、`package` 和可选的 `source_version`。能力声明同时记录 `implementation_version` 及 `command:ffmpeg`、`python:PIL` 依赖。响应采用 `api_version: 1`；`completed` 退出 0，可继续但未完成的状态退出 3，其余失败状态退出非零。实现移动时只修改 `video_extract.capabilities:CAPABILITIES`；兼容移动保持实现版本，行为改变时递增实现版本。入口、依赖或版本不匹配会返回确切维护位置，不会猜测替代工具。

`install apply` 在 Codex 安装根原子写入 `video-extract/install-receipt.json`，记录工程 revision、CLI/capability/schema/集成源码指纹和安装契约版本；收据提交失败会回滚本次新链接并恢复已备份手改。`install check` 将源码、revision、链接、依赖或收据漂移报告为 `recoverable_failure`。`install` 与 `workspace doctor` 都返回统一 command-response-v1 envelope；后者同时汇总能力和安装诊断，并可用 `--agents-root`、`--codex-root` 指向临时根完成隔离检查。

移动工作区后更新 locator，并重新执行 editable 安装、`install check` 与 `workspace doctor`。不要在 skill 或 agent 中写入项目、Media、资料库或 Python 环境的机器路径。
