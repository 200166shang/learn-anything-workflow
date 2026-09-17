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

## 状态、验证与迁移

Media package 是事实来源；资料库导出、SQLite 索引和播放视图是可重建派生物。正常运行只读取当前 schema。schema 1–4 package 和旧学习线程仅由显式、分批的迁移转换器读取；旧副本在切换后保持只读并退出发现。

~~~text
video-extract status PACKAGE --json
video-extract verify PACKAGE --json
video-extract library status --json
video-extract migration plan --batch NAME --legacy-package PACKAGE --legacy-thread THREAD --workspace WORKSPACE --json
~~~

旧 goal 名称、旧脚本命令和旧 Skill 别名不再是正常运行入口。转换器和回退副本只用于显式迁移与恢复；删除旧副本仍需单独确认。

## 安装

~~~text
brew install uv ffmpeg
uv sync
uv run playwright install chromium
uv tool install --force --editable /path/to/video-extract-core
video-extract install plan --obsidian-plugins-root /path/to/Vault/.obsidian/plugins --json
video-extract install apply --obsidian-plugins-root /path/to/Vault/.obsidian/plugins --json
video-extract install check --obsidian-plugins-root /path/to/Vault/.obsidian/plugins --json
~~~

`integrations/skills/`、`.codex/agents/`、Obsidian 插件和 Python 工具都只在本工程维护。`install apply` 在链接宿主入口前备份已有副本，并把已知旧 Skill 移到宿主发现目录外的时间戳备份；不会删除它们。`install check` 报告源码修订、内容指纹、依赖、安装漂移、旧入口复现和确切维护位置。

稳定能力入口由工程内唯一映射维护，调用方只保存能力 ID 和公开契约版本：

~~~text
video-extract capability list --json
video-extract capability check source.notes --json
video-extract capability run source.notes --request request.json --json
~~~

`source.notes` 的请求使用 `contract_version: 1`，包含 `action`（`prepare` 或 `finalize`）、`workspace`、`package` 和可选的 `source_version`。能力声明同时记录 `implementation_version` 及 `ffmpeg`、`ffprobe`、Pillow、faster-whisper 依赖。实现函数通过无副作用的 `__capability_contract__` 元数据声明 adapter I/O；check 同时验证注册声明、callable 形状、该元数据和类型注解。runner 传入普通 dict，因此输入注解必须能接受 dict（dict 或相应 Mapping 协议），不能要求某个更具体的 dict 子类；输出可使用 dict、Mapping 子类或 TypedDict。明确的非 mapping 类型拒绝，无注解则由协议声明并在 run 时强制验证真实返回。响应采用 `api_version: 1`；`completed` 退出 0，可继续但未完成的状态退出 3，contract 不兼容和其他失败退出非零。实现移动时只修改 `video_extract.capabilities:CAPABILITIES`；兼容移动不改变同一逻辑请求的 operation ID，行为版本只作为 provenance 和诊断事实。入口、I/O contract、依赖或版本不匹配会返回确切维护位置，不会猜测替代工具。

`install apply` 在 Codex 安装根原子写入 `video-extract/install-receipt.json`，记录工程 revision、CLI/capability/schema/集成源码指纹和安装契约版本；收据提交失败会回滚本次新链接并恢复已备份手改。`install check` 将源码、revision、链接、依赖或收据漂移报告为 `recoverable_failure`。`install` 与 `workspace doctor` 都返回统一 command-response-v1 envelope；后者同时汇总能力和安装诊断，并可用 `--agents-root`、`--codex-root` 指向临时根完成隔离检查。

新建 workspace 使用 schema v2，并把 `workspace.example.toml` 中的占位值替换为一次生成、之后不随改名、搬迁或路径映射变化而修改的 `workspace_id`。`project`、`results`、`sources`、`derived`、`local` 是相互独立的位置角色；相对路径随配置移动，绝对路径是显式声明的外置根。所有写入会在解析符号链接后重新核对声明边界。

本地文档可用 `source register` 或 `source import` 登记，源码目录用 `source register` 原处引用。`source verify` 固定读取一次 snapshot v1 的 `commit_id`，`source relocate` 只在内容版本相同且 `expected_revision` 匹配时更新本机位置映射。同一来源的新内容保留原 `source_id` 并新增 source version；文档正文、source package v6、完整清单和发布指针组成原子快照。旧 workspace v1 仍只供既有媒体流程读取；source v6 明确拒绝它并指向显式迁移，而不会猜测或自动转换。

移动工作区后更新 locator，并重新执行 editable 安装、`install check` 与 `workspace doctor`。不要在 skill 或 agent 中写入项目、Media、资料库或 Python 环境的机器路径。
