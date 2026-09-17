# VideoLearning：媒体、中文播客与来源笔记重构执行方案

状态：待执行。2026-09-17，根据用户决定和本机代码检查编写。

本文是交给执行 agent 的实施合同。按阶段完成代码、skill、agent 定义、验证和清理；不要重新讨论架构。文中的新增命令是实现目标，不能当作已经存在的命令运行。实现前读取适用的 AGENTS.md。实际文件变化优先于本文的现状快照。

## 1. 范围已经确定

必须做：

1. 收窄 extract-media，移出合成中文听觉版职责。
2. 建立 mandarin-audio skill，封装现有 pyVideoTrans podcast 用法和受管理的输出。
3. 将 video-learning 的笔记方法提炼成 source-notes，新增 source_notes_operator。
4. 更新已有 mandarin_netease_operator，保留名称、单 URL 默认工作方式、音频规格和网易云上传行为。
5. 两个 agent 和三个相关 skill 的新产物统一落到现有 VideoLearning 工作区。
6. 修复路径定位，整理完成后的正式入口，清理本次替代的旧 skill、重复说明和测试临时产物。

本轮不做：

- 不统一 ASR，不把笔记转写切换到 pyVideoTrans；不更换模型、供应商或播客 profile。
- 不新建转写 skill、通用 pyVideoTrans skill、总控 agent、学习 agent。
- 不修改 learning-learn、其 references、任何学习 thread.yaml 和已有问题笔记。
- 不建立个人 Git 仓库、不引入 submodule、不提交、不推送；允许修改现有代码仓库中的本次相关文件。
- 不搬迁整个工作区、pyVideoTrans 项目或现有 Media 包。
- 不构建全格式文档解析平台、OCR 服务、通用 provider 框架或后台任务服务。
- 不自动执行真实云端合成、网易云上传或批量重新生成资料来测试重构。

“清爽”的完成含义：每种新业务只有一个正式入口；共享规则只有一个维护位置；输出有固定归属；旧 skill 不留转发壳。现有数据的读取支持和真实任务恢复数据不属于可随便删除的垃圾。

## 2. 已确认的现状

当前工作区：`/Users/syz/VideoLearning`。

| 对象 | 当前位置/事实 |
|---|---|
| 工作区配置 | `/Users/syz/VideoLearning/workspace.toml` |
| 代码项目 | `/Users/syz/VideoLearning/video-extract-core` |
| Media | `/Users/syz/VideoLearning/media` |
| Vault | `/Users/syz/VideoLearning/学习系统` |
| 生成笔记区域 | Vault 内 `资料库/网站视频转录` |
| 提取 skill | `/Users/syz/.agents/skills/extract-media` |
| 旧笔记 skill | `/Users/syz/.agents/skills/video-learning` |
| 播客 agent | 代码项目内 `.codex/agents/mandarin-netease-operator.toml`；内部 name 为 `mandarin_netease_operator` |
| 网易云 skill | `/Users/syz/.codex/skills/netease-music-cli/SKILL.md` |
| pyVideoTrans CLI | `/Users/syz/code/pyvideotrans-runtime/cli.py` |
| 当前播客 Python | `/Users/syz/code/pyvideotrans/.venv/bin/python` |
| 已安装工具 | `video-extract`，目前为 uv tool editable 安装 |
| 本机工作区 locator | `~/.config/video-extract/config.toml` |

extract-media 中 `/Users/syz/code/video-extract` 已失效。video-learning 已用 WorkspaceConfig。两者不能继续各用一套路径规则。

笔记主流程 `_default_transcriber` 使用现有 faster-whisper small、自动语言识别，产出 SRT；`EnsureDependencies.transcriber` 已是注入点。低层旧 `transcribe` 命令默认语言为 zh，不要误用它作为新笔记流程的默认调用。

pyVideoTrans 的普通 `stt` 与定制 `podcast` 是不同执行路径。本轮只沿用现有 podcast，不读它的私有 ASR 产物，不增加两条流程之间的转写交换。

`obsidian_export.py`、`library.py`、`playlists.py` 已有存储派生工具。本轮复用，不建对应 skill。

检查时已有非本轮修改：`convert_voice_to_article.py`、`tests/test_cli_contract.py`、`video_extract/cli.py`，以及小鹅下载相关新增文件。执行者先检查最新状态，保留这些改动；不得 reset、覆盖或删除它们。特别保留 xiaoe 命令和测试。

## 3. 最终依赖图

```text
mandarin_netease_operator
  ├─ extract-media → video-extract 媒体提取
  ├─ mandarin-audio → 现有 pyVideoTrans podcast / 原生中文音频规范化
  └─ netease-music-cli → ncm-cli

source_notes_operator
  ├─ extract-media → 仅在远程媒体需要获取时使用
  ├─ video-extract source import → 登记本地材料
  ├─ video-extract notes prepare → 复用文本 / 现有笔记 ASR / 候选证据
  ├─ source-notes → 理解、证据选择、笔记写作
  └─ video-extract notes finalize → 校验、导出、索引、必要时刷新集合播放视图
```

这是两个平级的专门 agent。agent 选择流程和偏好，skill 描述该能力的工作标准，工具执行机械步骤。skill 不再互相编排。不要在 agent 和 skill 内重复下载恢复规则、TTS 参数、笔记检查规则。

## 4. 存储：沿用工作区和 package，不生成零散输出

```text
VideoLearning/
  workspace.toml
  video-extract-core/
    .codex/agents/
      mandarin-netease-operator.toml
      source-notes-operator.toml
  media/
    items/<platform>/<stable-id>/
      manifest.json
      media/                       # 新包的来源音视频
      subtitles/                   # 已有字幕及必要的来源转写
      source/                      # 仅本地文本来源需要，按需创建
      evidence/                    # 候选、选择记录、采用图片，按需创建
      notes/notes.zh-CN.md
      listening/zh-CN/              # 播客或原生中文听觉版
      publishing/netease/           # 上传文件和回执
      export-manifest.json         # 现有导出器拥有
    collections/                   # 现有集合记录
    catalog/                       # 现有可重建索引
    playback/                      # 现有播放视图
    sessions/                      # 现有鉴权会话
  学习系统/
    资料库/网站视频转录/             # 继续使用已有配置，不重命名
    ...                            # 其余内容保持不变
```

规则：

- 新任务默认只在解析出的 `media/items` 内建包。网络来源沿用 canonical identity；本地文件沿用内容指纹。同一来源复用同一包，禁止按日期/标题再建一套。
- 显式传入已有 package 时使用其 manifest 中的路径，不改 ID，不复制成新包，不覆盖 schema 版本。已有工作区内的旧包原地使用。
- 工作区外的本地音视频、SRT、Markdown、TXT 作为输入导入受管理包；不修改或删除原文件。传入外部旧 package 时，本轮仅报告需单独导入，不静默迁移整包。
- 新文件采用本节布局；旧包继续通过 artifacts 记录定位。路径兼容集中在一个解析函数，不在每个 skill 写旧路径分支。
- 包内引用保存相对路径；API/CLI 可返回解析后的绝对路径供调用，不用绝对路径当身份键。
- 必要目录按需创建。仅提取音频不生成 notes/evidence，写文档笔记不生成空视频/截图目录。
- 不创建第二份 job manifest 重复记录下载、ASR、TTS 状态。复用包 manifest、pyVideoTrans 自己的状态及下述上传回执。
- 同包修改 manifest 和新笔记流程用同一个包级写锁，锁竞争返回可重试的 busy；不同包可并行。锁放系统临时区，以 package 解析路径的摘要命名，使用系统文件锁，关闭后释放，避免遗留永久“运行中”状态。播客内部恢复仍由其原实现负责。
- 新笔记流程保留导入/下载的来源媒体，不自动清理其他流程可能使用的文件。用户明确请求媒体清理不在本轮任务内。

### 4.1 播客输出

- 合成分支继续由 pyVideoTrans 独立拥有 `listening/zh-CN/`，包括标准 MP3、生产报告和私有恢复资料。
- 原生中文分支无需 TTS。由 mandarin-audio 的 helper 用 FFmpeg 规范化到 `listening/zh-CN/podcast.zh-CN.mp3`，生成简短安全的 `production-report.json`，明确 `mode=native`、输入指纹、输出规格。不得伪造 pyVideoTrans run manifest。
- 两条分支最终都满足现有 agent 要求：MP3、48000 Hz、单声道、64 kbps、有效时长。规范音频文件名固定。
- 上传所需标题副本放 `publishing/netease/<安全处理的原视频标题>.mp3`。优先硬链接，跨文件系统才复制；不能移动规范音频。
- `publishing/netease/receipt.json` 保存非敏感的 source identity、音频 SHA-256、上传文件名、远端 ID、是否确认可见、确认时间。回执不是替代云盘核对的凭据。
- 不把输出放桌面、项目根、输入文件旁、随机 timestamp 目录或另一个 Media 根目录。

### 4.2 中间文件

- 已采用的图片、证据选择记录、来源转写、manifest、生产报告、恢复所需文件属于必要产物，保留。
- 候选缩略图、未采用截图、contact sheet、notes_input 等是可重建工作资料：finalize 全部成功后仅清理由本次笔记准备器明确登记的临时文件，并使终态验证不再依赖它们。保留覆盖清单/排除理由于同一份证据记录中，防止删完后无法检查笔记覆盖。
- 终态证据记录包含采用项的 ID、来源定位、图片相对路径/指纹、review_mode 和知识块覆盖结果；准备期与终态验证分开，清理候选不允许让终态回退到 candidates_ready。阶段指纹失效后才重新准备，不因临时文件已清理而重跑完成任务。
- 失败或 awaiting_ai 时保留恢复需要的文件。重新运行不得覆盖已完成的人工作品或重复计费。
- 不读取或清理 pyVideoTrans 的 `run.private.json`、`private/`；需要维护时由该项目自己的工具负责。
- 本轮测试使用系统临时目录并在结束清理。不要在真实 media/items 留 fixture、test、demo 包。
- 不清理已有 `media/migration/quarantine`、会话、旧工作目录或用户文件；它们的来源未在本轮充分审计。

## 5. 路径配置与发现

继续使用 WorkspaceConfig，不另建 workspace 框架。

### 5.1 工作区

保留现有发现顺序：显式配置 > VIDEO_EXTRACT_WORKSPACE > 当前目录祖先 > 本机 locator。

本次新增命令和改动的媒体 plan/ensure 命令统一支持 `--workspace PATH`。agent 开始时执行 workspace show，固定返回的 config 路径；后续命令显式传同一个 workspace，避免换 cwd 后切换工作区。旧的不相关命令无须为了统一样式全部改写。

工作区 locator 继续只保存该机器的 workspace.toml 绝对位置。失效时返回明确错误，不自动创建新工作区。维持 project/media/obsidian 处于当前根目录内的限制，本轮不支持外置根目录重构。

### 5.2 pyVideoTrans 安装位置

在现有 workspace.toml 增加可选配置，schema_version 保持 1；旧配置缺少该节时媒体和笔记命令仍正常，只有播客 helper 报依赖未配置。

```toml
[tools.pyvideotrans]
python = "/Users/syz/code/pyvideotrans/.venv/bin/python"
cli = "/Users/syz/code/pyvideotrans-runtime/cli.py"
```

这是当前机器的实际配置值，只能出现在本机配置和本文现状说明中。维护中的 skill/agent/helper 代码不得写死这些值。允许绝对路径和相对 workspace.toml 的路径；仅这两项允许位于工作区外。不要降低 media/Vault 的边界检查。配置内不保存 API key。

WorkspaceConfig 解析这两项，workspace show --json 返回解析后的 `tools.pyvideotrans.python/cli`。helper 通过这个接口取得位置，不另写 TOML parser、不搜索全盘、不复制解析优先级。

`mandarin-audio/scripts/run.py` 是薄适配器：使用当前可用 Python 运行，读取 workspace show 的 JSON，再以 argv 数组启动配置的 Python 和 CLI。保留 profile `alibaba-podcast-tts-throughput`，已有 run 走 resume；不复制 pyVideoTrans 的 ASR/翻译/TTS/重试算法。

扩展 workspace doctor：报告本次相关配置路径存在性、已安装 video-extract 是否能加载预期项目、新旧 skill 活跃入口、agent 依赖缺失。配置存在但指向丢失 runtime 时报告错误；未配置播客依赖时标明该能力不可用，不阻断只做笔记。

项目移动后 editable 安装仍可能失效，迁移说明必须包含重新安装 `uv tool install --force --editable <解析出的项目>`。本轮不要移动项目来测试；临时工作区覆盖定位测试即可。

### 5.3 skill 和 agent 定位

- extract-media 继续在 `~/.agents/skills/extract-media` 原地维护。
- source-notes 和 mandarin-audio 同样放 `~/.agents/skills`，与本次重构对象一致。不要同时复制一份到 ~/.codex/skills。
- netease-music-cli 保持原安装位置和独立职责。
- agent 指令用 skill 名称和当前会话的 skill catalog 定位。未加载时在已知用户 skill 根目录按确切名称查找；不硬编码 `/Users/syz`，不全盘搜索。仍缺失则返回明确依赖错误。
- 两个 agent 定义继续放在现有代码项目 `.codex/agents/` 中。不要移动现有 agent 定义或额外复制到工作区根。保持用户现有入口使用方式。
- 新 agent 沿用现有 TOML 格式，内部 name=`source_notes_operator`，model=`gpt-5.6-sol`，model_reasoning_effort=`low`。更新旧 agent 不改其 name/model/effort。
- 不假设文件写完等于宿主已经加载；最终核对当前宿主的实际 agent 发现方式，必要时报告一次刷新/重开项目操作。若需注册，按宿主实际支持的配置完成，不发明字段，不创建同名第二份定义。

## 6. 媒体提取：停止知道 pyVideoTrans

主要修改 planner.py、media_workflow.py、媒体请求验证和 extract-media skill。

- plan/ensure 只处理媒体获取。`--language zh-CN` 表示请求已有中文音轨；找不到时返回结构化 `unavailable` 和 `reason=missing_requested_language_track`，提供已知来源语言，既不触发合成，也不假装得到中文音频。
- 保留原生中文优先、普通来源音频、视频、正式/自动字幕元信息等既有提取功能。
- 从新媒体响应删除 `mandarin_audio.mode=external_pyvideotrans`、`pyvideotrans_podcast` 和 `awaiting_external` 编排；输出通用的音轨可用性和来源语言信息。旧 manifest 中这些字段可以被读取，但不驱动新动作。
- 媒体组合请求按每个 kind 给出完成/不可用结果；某种音轨不可用不谎称其他请求失败，也不私自转换其语言或丢失其他 media scope。
- 媒体 ensure 更新 manifest 时合并已有 artifacts/stages；不要因补下载把笔记 schema 降级、清空笔记状态或删除 listening/publishing。
- 视频笔记先由 agent 要求提取所需媒体和字幕。notes prepare 遇到缺媒体时返回 `needs_input`；不得反向调用下载器或打开浏览器。
- 更新 extract-media 的描述：URL/本地媒体获取、已有音轨和字幕。删除中文听觉版生成触发词和 chinese-audio reference 链接。
- acquisition 与三个平台 reference 留在 extract-media，仅维护一份平台恢复说明。

## 7. 来源准备与笔记工具接口

在 video-extract 项目增加一组简单命令，复用现有函数。不要新建独立服务或为每一步创建 skill。

### 7.1 `video-extract source import INPUT [--package PACKAGE] [--title TITLE] --workspace CONFIG --json`

用途：将本地音视频、SRT、Markdown、UTF-8 TXT 登记到受管理包。复用现有本地媒体提取和指纹逻辑。

- 新输入按本地内容指纹分配包；已有 package 直接打开。`--package` 用于明确把配套 SRT/文档加入指定的受管理来源包，不能猜测两个不同文件属于同一视频。
- 支持输入类型：已有媒体包、本地音视频、SRT、Markdown、TXT。PDF/DOCX/网页正文自动解析本轮不实现；agent 明确说明需要可读文本，可消费用户已提供的文本，不创建伪成功包。
- Markdown 本地图片只导入实际引用的安全可读文件，保持或重写包内相对链接；不递归复制整目录。不支持的附件明确列出。
- 新包沿用 schema v5，增加可选 `source_kind=video|audio|transcript|document` 和必要 artifact 字段；更新 schema 和消费者。旧包缺字段时从已有有效 artifacts 推断，不批量重写。
- 新的纯文本/SRT 导入包使用 `request.type=source`，登记 source_kind；扩展 v5 schema 的 request 分支。媒体包保留 `request.type=media`。验证器根据请求与真实产物分支校验，纯文档不得为了通过媒体校验而伪造 audio/video 请求。笔记状态登记在 stages 中，不用 notes prepare 覆盖原始 media request。
- source document 保存到包内 `source/document.md` 或 `.txt`；SRT 保存到 `subtitles/transcript.source.srt`。已有正式字幕可直接登记为 transcript_srt，避免再复制一份相同文本。
- 来源身份、原始语言/未知、字幕类型、导入内容指纹保存在现有 manifest/provenance。内容发生变化不能无条件复用旧完成状态。
- 返回 package、source_kind、已登记 artifacts 和缺失输入。所有写入在包级锁内，原子更新 manifest。

### 7.2 `video-extract notes prepare PACKAGE --workspace CONFIG --json`

只消费本地已登记输入。现有笔记 ASR 保持原实现和默认值。不要调用低层默认中文的兼容命令。

处理分支：

| 输入 | 文本准备 | 证据 |
|---|---|---|
| 视频 | 复用可靠字幕；缺少时从已有媒体取得音频并运行现有 ASR | 调用现有抽帧器准备候选 |
| 音频 | 复用转写；缺少时运行同一现有 ASR | 时间戳，无视频截图要求 |
| SRT | 校验后直接使用 | 时间戳；无关联视频就无需截图 |
| Markdown/TXT | 直接读取正文 | 标题/段落定位，按需引用已有原图 |

来源笔记直接读来源语言文本，不生成完整中文字幕中间层。时间轴必须对应来源媒体。来源文本和中文播客改写稿不混用。

复用 `_default_transcriber`、现有证据候选代码和 validator 的相关部分；抽出缺少的本地准备函数。不要将调用旧 `ensure(URL, notes_zh)` 包装一下充当新命令，否则仍有隐藏下载和重复编排。

JSON 状态固定为 `needs_input`、`awaiting_ai`、`ready`、`failed`、`busy`。含 package 和适用的 `action`、input/output 的包内路径、恢复 argv。awaiting_ai action 使用 `evidence_select` 或 `notes_write`。argv 是数组，不拼 shell 字符串。重复执行根据实际 artifacts 推进。

新包固定 artifact 映射：`transcript_srt` 指向已采用的字幕文件；新 ASR 写 `subtitles/transcript.source.srt`；`source_document` 指向 `source/document.md|txt`；`candidate_json=evidence/candidates.json`、`approved_json=evidence/approved.json`、`contact_sheet=evidence/contact-sheet.jpg`、`notes_input=notes/notes_input.md`、`notes=notes/notes.zh-CN.md`。图片放 evidence 下，由记录指向实际相对路径。工具适配旧抽帧函数的输出，不让它把来源视频再复制到第二个 source/source.mp4。

视频要求模型选择证据；音频/文本不伪造候选和人审。review_mode 默认为 model_only。覆盖清单和排除理由沿用并整理现有记录结构，不额外维护第二份关系图。

### 7.3 `video-extract notes finalize PACKAGE --workspace CONFIG --json`

一个确定性收尾入口，顺序固定：

1. 按 source_kind 验证来源、引用、笔记覆盖与采用的图片。
2. 调用现有 Obsidian exporter，仅写配置的 generated 区域。
3. 更新现有 library index。
4. 仅在该包影响的 collection catalog 有变更且包含视频时调用现有 playlist 更新。
5. 完整成功后清理本次已登记的可重建工作资料，再验证终态仍有效。

返回各阶段结果、package、最终 note/export 路径、复用情况、是否清理及 blocker。中间一步失败不宣称整个笔记流程完成，后续重跑可复用已成功阶段。不得全库 rebuild 代替单包操作。

导出器继续处理已有笔记冲突，用户编辑过的导出文件不覆盖。对音频/文本适配来源链接：没有视频就不写不存在的“打开源视频”。继续使用 manifest 中实际 artifacts，索引可记录新文档正文和无时间戳笔记，不要求空视频。

verify 的 notes_zh 仍作为产物验证标识存在。视频与无视频分支都保留实质质量标准：视频有用画面需证据；音频需真实时间引用；文档需真实段落/章节定位。不能仅“文件非空”即通过。完成记录存摘要/指纹与覆盖状态，临时候选删除后依靠保留的采用证据验证。

不新增导出 skill 或索引 skill。source-notes 不记导出路径；source_notes_operator 只调用 finalize，不再复制三条收尾命令。

## 8. 三个 skill 的最终内容

### 8.1 extract-media：原地改写

维护媒体请求映射、包定位、plan/ensure、平台恢复和媒体 verify。只报告取得的媒体与不可用项。中文合成请求交回编排调用方，不调用 mandarin-audio。

删除 `references/chinese-audio.md` 的责任在新 skill 和现有 agent 验收通过后执行；更新 evals，使英文来源请求中文音轨的预期变为不可用，而不是自动合成。

### 8.2 mandarin-audio：新建

```text
~/.agents/skills/mandarin-audio/
  SKILL.md
  scripts/run.py
```

只有内容足够长且确有条件分支时再增加一个 reference；不要生成空 README、模板或未使用目录。

输入是工作区 package 中已取得的来源音频或原生中文音轨。skill 不下载、不上传、不转写给笔记流程。

helper 接口：

```text
python <skill>/scripts/run.py PACKAGE --workspace CONFIG --json
python <skill>/scripts/run.py PACKAGE --workspace CONFIG --check --json
```

默认执行自行判断原生中文规范化、英文合成、已有结果复用或 resume。`--check` 只检查配置和安全产物，不触发网络/制作。来源语言未知或非受支持语言返回明确 unsupported，不能启动云调用。

分支依据是所选实际音轨的 provenance：优先有效的 native_chinese_track，再检查 source_audio 的语言；不能因为视频原始语言是英文就忽略已提取中文轨。`--check` 对未完成任务可返回 pending/missing_artifact，不能仅凭运行依赖存在报告音频完成。

先按 manifest 找现有音频，不硬编码所有旧包都叫 media/audio.source.m4a。校验音频输入；输出目录固定为包内 listening/zh-CN。根据安全报告和实际音频验证复用，不靠文件存在判断完成。

现有 podcast profile、48k 单声道 64kbps、自然节奏听觉版、失败恢复和隐私边界从旧 reference 迁入。实现仅转发现有 CLI 并规范化安全结果；不读取 private 状态来模拟 resume。对子进程输出按安全字段返回，错误脱敏，不原样倾倒 provider 输出。

不自动重提结果不明、可能重复收费的提交；继续查询已知任务或使用标准 resume。如仍需要 `--retry-uncertain`，要求用户针对重复计费风险作出明确决定。旧 agent 的“无进展就重试不确定提交”条款应改为本规则，避免与 runtime 恢复语义冲突。

### 8.3 source-notes：从 video-learning 提炼

```text
~/.agents/skills/source-notes/
  SKILL.md
  references/evidence-notes.md
  assets/note-template.md          # 只在实际写作需要时保留
```

description 明确：基于已经可读取的来源文本、转写及可选视觉证据，生成中文来源笔记；不是 URL 下载入口或学习对话记录器。

主体包含：检查本地准备输入；理解主线；划分并覆盖知识块；选择有用证据；完成 notes_write；调用工具验证/继续。输入不足时返回所缺材料，不自己下载或选择 ASR provider。

工具返回的 package/output 是存储位置，不自行选择 Vault 或随机输出。写作核心来自旧 evidence-notes，保留模型审查标记、采用图片全部引用、知识块覆盖/排除和来源定位。

模板改为可根据材料组织的笔记结构，移除强制“必须掌握/了解即可”、固定结尾回顾和没有内容也要求填写的章节。保留材料主线、重要机制、例子、来源引用和不确定之处；不要把完整讲解压成提纲。

只保留笔记规则；旧 acquisition.md 和四个平台 adapter 的下载/恢复规则不搬入。唯一的平台操作说明在 extract-media。

## 9. 两个 agent 定义

### 9.1 mandarin_netease_operator：更新已有文件

保留单个授权 URL 为默认输入；多链接仍明确要求一次一个，不擅自批量。流程改为：

1. 定位 workspace，按名称加载 extract-media、mandarin-audio、netease-music-cli。
2. 读取来源元数据，优先获取已有中文音轨；若无中文轨且确认英文，获取原始音频；其他语言/未知明确报告不支持。
3. 得到同一个 canonical package；调用 mandarin-audio，得到规范 MP3 和安全生产报告。
4. 在 publishing/netease 准备原标题文件；按现有 ncm-cli 实际 help 查询云盘，优先回执 ID，再核对文件名和时长。文件名相同不能覆盖或删除云端条目。
5. 无已确认对应条目时上传；上传响应、status 与 cloud list 必须交叉核对。状态不明先查询，不能盲目重新上传。
6. 原子写入安全 receipt；仅 MP3 规格通过且远端条目确认可见才算完成。

保留原有禁止 UI 上传和不传不支持的 --userInput 条款。当前 netease-music-cli 的“所有非播控命令都加 --userInput”与已验证 cloudupload 使用冲突：仅把共享 skill 该规则改为“按子命令 help，仅在支持时传入”，补充云盘上传/查询的适用描述。不改登录实现或扩展其他音乐功能。

云盘查询处理分页；时长先统一为秒，匹配容差为 max(2 秒, 本地时长的 1%)。若服务返回缺少时长或多个无法区分的同名项，保留不确定状态，不据此宣称精确匹配、不覆盖远端也不盲目再上传。上传副本的标题只做文件名必需字符处理；无法得到可靠原标题时报告缺失元数据，不自行翻译或生成标题。

保持此前固定流程内的上传授权，不要求用户每个正常阶段重复确认。未知付费提交重试、登录、额度或业务选择按对应边界处理。最终报告原链接、标题、规范 MP3 路径、音频规格、云盘文件名、确认状态和 ID。

### 9.2 source_notes_operator：新增文件

默认接受一个视频 URL、一个现有包或一份本地材料。已有 collection 支持用户指定章节/条目选择，沿用现有完整 catalog，不实现新爬虫；没有选择范围时先明确范围，不静默只处理第一项。

流程：

1. 固定 workspace，识别输入类型和已有可复用 package。
2. 远程媒体通过 extract-media；本地材料通过 source import；已存在包直接复用。
3. notes prepare，缺输入时补齐对应来源；不转向 pyVideoTrans。视频为了证据获取视频，用户明确只提供音频时按音频模式处理。
4. 遵循 source-notes，处理 evidence_select/notes_write，按返回 argv 恢复到 ready。
5. notes finalize，完成后报告包、来源笔记、导出路径、来源类型、证据数量/模式、保留策略、未解决问题。

正式字幕可用时不再 ASR；现有包有效笔记未变化时复用，不每次重写。agent 不执行学习记录，不修改 thread.yaml，不要求用户先启动其他 agent。

## 10. 收尾和旧入口处理

新实现通过验证后，一次性切换活跃入口：

1. 删除旧 `~/.agents/skills/video-learning/`，包括已被提炼或废弃的 reference、adapter、旧 eval。先将仍有价值的场景迁入新 skill 验证用例，确保不存在唯一内容丢失。
2. 删除 extract-media 中旧 chinese-audio reference，所有活跃调用改为 mandarin-audio。
3. 不创建 video-learning → source-notes 的转发 skill、旧命令 wrapper 或同名别名。
4. `plan/ensure` 对新工作只保留媒体请求。移除其 `--goal notes_zh|podcast_zh` 的公开执行分支，笔记改走 notes 命令，播客改走 mandarin-audio；不留下第二套 end-to-end 编排。CLI 对旧参数正常报用法错误，迁移说明指出正式替代入口即可。
5. `status/verify` 对已有 schema 1–5 和 goal 名称的读取/验证保留，集中在现有 reader/validator；这是保护用户已有产物，不是继续提供旧业务入口。旧 helper 若仍被现有 ASR/抽帧调用，保留作内部实现，不为清爽而删除正在使用的文件。
6. 修改 README 的当前使用说明，移除正文中已失效的操作命令。更新 package-contract、workspace-migration 和有关 ADR 的状态，说明哪些旧决策已由本次方案取代；不要让两篇“当前规范”相互矛盾。
7. 检查活跃 agent、skill、helper、README 和当前配置中的失效路径/旧 skill 引用。历史 ADR、测试的模拟路径、本文现状记录不必为了字符串清零而删除。
8. 不产生 `.bak`、`-old`、`v2` skill 目录、临时 migration wrapper 或多份最终方案。本文件末尾记录执行结果即可。

## 11. 实施顺序与阶段出口

### A. 基线与工作区接口

- 阅读 workspace.py、cli.py、contracts.py、package_paths.py、已有 agent、三个现有 skill 和 package-contract。
- 确认现场改动，不覆盖小鹅功能。先运行既有相关测试记录基线失败。
- 实现 tools.pyvideotrans 配置解析、新命令的 workspace 参数、包路径检查及包级写锁。
- 出口：临时工作区可从任意 cwd 定位；无播客配置时纯媒体/笔记仍可运行；配置不泄露凭据。

### B. 中文播客职责切分

- 修改媒体 plan/ensure 的纯提取语义及验证。
- 新建 mandarin-audio/helper，更新现有播客 agent，补最小 ncm skill 参数修正。
- 出口：英文分支调用现有 podcast argv 正确；原生中文规范化符合规格；输出和上传副本均位于同包；恢复不从头执行。

### C. 本地来源与笔记管线

- 实现 source import、notes prepare/finalize，抽取复用现有 ASR/证据/验证/导出函数。
- 按 source_kind 调整 schema/validator/library/export；统一新包 artifacts，保留旧数据 reader。
- 出口：本地视频、音频、SRT、Markdown/TXT 在临时工作区可分别走完本地准备和模拟 AI 写入后的完整 finalize；prepare 绝不下载。

### D. skill 与 agent 切换

- 建立 source-notes 和 source_notes_operator，更新 extract-media 的 descriptions/references/evals。
- 对 agent TOML、skill frontmatter 和所有引用做验证，确认发现路径。
- 出口：给执行者最小输入就能明确下一步、输出在哪里以及什么算完成；agent 不复制 skill 的内部操作。

### E. 移除旧入口与验收

- 按第 10 节删除本次替代对象和公开旧执行路径，更新测试预期及文档。
- 跑第 12 节验收；只对本次修改导致的失败继续修复，不凭空扩大重构。
- 出口：全部本轮验收通过；已有真实数据未移动/重写；未执行外部付费/上传；现场原有修改完整保留。

不要只改 Markdown 就宣称职责已解耦；也不要在新流程未通过测试前删除唯一可用入口。删除是阶段 E 的最终切换，本次交付结束时不留并行旧入口。

## 12. 必须执行的验证

遵循现有 unittest 风格。新增测试放现有 tests 目录；用临时目录、短 FFmpeg 合成媒体、fake ASR、fake pyVideoTrans/ncm 子进程。测试可验证编排，不需要运行真实模型或触发云 API。skill 文案检查不替代行为测试。

| 验收 | 必须观察到的结果 |
|---|---|
| 路径解析 | 不同 cwd + 明确 workspace 指向同一个包根；路径含空格可用；失效 locator 不另建目录 |
| 配置可选 | 未设置 pyVideoTrans 时媒体/笔记可用，播客 check 返回缺依赖 |
| 纯媒体职责 | 中文轨缺失返回 unavailable；媒体层不启动 pyVideoTrans、不生成 awaiting_external |
| 组合提取 | 请求多个 media kind 时保留已完成项和各项不可用原因 |
| 原生中文 | 不调用 TTS，MP3 经 ffprobe 满足规格且位于 listening/zh-CN |
| 英文播客 | argv 的 Python/CLI 来自配置，输出目录正确，profile 不变 |
| 恢复 | 已完成结果复用；已有 run 使用 resume；不自动传 retry-uncertain |
| 上传 | 副本在 publishing；存在已确认条目不上传；状态不明不重传；安全 receipt 可恢复 |
| 下载复用 | 同来源不新建第二个标题包；补媒体不抹掉 notes/listening/publishing |
| 来源导入 | 本地输入不变；重复导入复用；配套 SRT 仅在显式关联时并入指定包 |
| 转写边界 | 正式字幕存在时 ASR 调用数为零；缺字幕时调用现有笔记 ASR；不调用 pyVideoTrans STT |
| 本地准备 | notes prepare 不联网、不调用下载器；缺输入返回 needs_input |
| 视频笔记 | adopted 图片均能解析且在文中引用；覆盖缺项会失败；模型选择不标人审 |
| 无视频笔记 | 音频/SRT 有时间证据；文档用段落定位；不制造视频文件/链接/截图 |
| 完成幂等 | 第二次 finalize 复用结果，不重复改写笔记/导出；出错可恢复 |
| 导出冲突 | 用户修改过的导出文件不被覆盖；学习目录哈希保持不变 |
| 清理 | 只删除登记的可重建临时文件；删除后 verify 仍通过；失败任务保留恢复资料 |
| 同包并发 | 两个写入者不会丢失 manifest 更新；busy/恢复可观察 |
| 旧数据 | 代表性的 schema 1–5 fixture 可读取、按旧 artifacts 定位；不自动改 schema |
| 入口切换 | 新 skill 可发现；旧 video-learning 无活跃安装副本；两个 agent name 正确 |
| 回归 | workspace、media request/reuse、notes/CLI、export、library、playlist 和现有小鹅相关测试通过或明确记录原有失败 |

典型命令（执行时按实际安装和新增文件名调整，不改变验收内容）：

```text
uv run --project <PROJECT> python -m unittest discover -s <PROJECT>/tests
video-extract workspace show --json
video-extract workspace doctor --json
python <skill-creator>/scripts/quick_validate.py <SKILL_DIRECTORY>
```

先跑受影响测试，再跑非 live 的正常测试集。禁止将 tests/live 的付费/外部写操作当默认验收。若现有发现规则包含 live，显式排除该目录。真实 workspace doctor 只读；不要对真实 workspace 自动 rebuild --apply。

用户后续在真实输入上触发 agent 时才执行真实制作/上传。交付报告必须区分“离线集成已验证”和“真实服务尚未调用”，不声称远端成功。

## 13. 文件改动清单

必须修改或新增的对象：

- `workspace.toml`：新增本机 pyVideoTrans 路径配置。
- `video_extract/workspace.py`、`workspace.example.toml`：解析和示例；模板用占位路径，不复制本机凭据。
- `video_extract/cli.py`：正式新命令、workspace 参数、删除旧公开 goal 执行入口。
- `video_extract/planner.py`、`media_workflow.py`：纯提取语义。
- `video_extract/contracts.py`、相关 schemas、`validate.py`：source_kind、artifact 路径和来源类型校验。
- 本地来源导入/笔记编排模块：优先复用/重构 orchestrator；必要时新增 `source_import.py` 和 `notes_workflow.py`，不要保留同功能第二份流程。
- `obsidian_export.py`、`library.py`：按来源类型读取实际产物和链接；`playlists.py` 仅在适配需要时改动。
- 包级锁：一个共享 helper，媒体/导入/笔记写入统一使用。
- `~/.agents/skills/extract-media/`：修改并移除 chinese-audio reference。
- `~/.agents/skills/mandarin-audio/`：新增 skill 和薄 helper。
- `~/.agents/skills/source-notes/`：新增提炼后的 skill。
- `~/.agents/skills/video-learning/`：验收后删除。
- 现有 `.codex/agents/mandarin-netease-operator.toml`：更新。
- 新 `.codex/agents/source-notes-operator.toml`：创建。
- `~/.codex/skills/netease-music-cli/SKILL.md`：仅云盘使用范围和按 help 传参规则的最小修正。
- 对应测试、README、package-contract、workspace-migration 和受影响 ADR。

不默认修改 pyVideoTrans 项目内部。helper 能完成的路径和输出适配不应引入其新业务实现；若确有 runtime 缺陷阻断本合同，先准确记录证据并局部修复，不能趁机统一 ASR。

## 14. 交付给用户

最终回复给出：

1. 两个 agent 的使用入口和各一个简短调用示例。
2. 三个 skill 的最终位置及职责。
3. 一个清晰的包目录示例，指出规范音频、上传副本、来源笔记和 Vault 导出位置。
4. 新增、修改、删除的主要文件，旧入口已清理的结果。
5. 已运行验证、现存阻碍、是否需要宿主刷新；明确未进行真实云端合成或网易云上传。

在本文件下方追加简短“执行结果”，写真实验收结果和必要偏差，不再生成多份总结/迁移日志/新方案文件。

实施结束的判定：用户可以在现有项目使用 mandarin_netease_operator 或 source_notes_operator，所有新产物受 VideoLearning 管理；没有重复业务入口、失效路径或伪装完成的阶段；学习系统和原有数据保持完整。

## 执行结果（2026-09-17）

已完成：

- `plan/ensure` 的公开入口已收窄为纯媒体请求；缺失语言轨返回 `missing_requested_language_track`，媒体层不再返回 pyVideoTrans pause。
- 新增 `source import`、`notes prepare`、`notes finalize`。本地视频、音频、SRT、Markdown/TXT 进入 canonical package；笔记沿用现有 faster-whisper，不调用 pyVideoTrans STT。
- 新增包级系统锁；媒体、来源导入和笔记写入均使用同一锁。笔记 finalize 验证、导出、清理登记的可重建工作资料并更新索引。
- 视频候选标准化到 `evidence/`；音频、SRT 和文档不制造视频或截图。无视频导出不生成“打开源视频”链接。
- WorkspaceConfig 已支持可选 `tools.pyvideotrans`，真实 workspace 配置完成；保留 venv Python 的 symlink 路径。workspace doctor 会独立报告播客能力可用性。
- `extract-media` 已收窄；新增 `mandarin-audio` 与 `source-notes`。旧 `video-learning` 和 extract-media 的 `chinese-audio.md` 已删除，没有转发 skill。
- `mandarin_netease_operator` 已更新；新增 `source_notes_operator`。两个定义保留在项目 `.codex/agents/`，名称、模型和 low effort 均通过 TOML 解析检查。
- `mandarin-audio` helper 支持原生中文规范化、英文 podcast 新建/恢复、只读检查和实际 MP3 规格校验。没有加入自动 `retry-uncertain`。
- 现有 schema 1–5 的 reader、内部 goal validator 和旧 deterministic 实现保留，仅移除公开 `plan/ensure --goal`，用于保护已有 package。
- README、package contract 和被取代 ADR 已更新；`video-extract` 已从当前项目重新 editable 安装。

验证结果：

- `uv run --with pytest pytest -q tests --ignore=tests/live`：99 passed，11 subtests passed。
- 三个 skill 均通过 `quick_validate.py`。
- `python -m compileall -q video_extract`、`git diff --check` 通过。
- 真实 `workspace show` 正确返回当前 project、Media、Vault 和 pyVideoTrans venv/CLI；真实只读 `workspace doctor` 返回 ok，migration_regression=0、missing_paths=0、broken_images=0、podcast_capability.available=true。
- 离线测试覆盖文档/SRT 来源、笔记 finalize、无视频导出、证据目录标准化、原生中文 48 kHz 单声道约 64 kbps MP3、旧 package validator 和媒体语言不可用语义。

有意未执行：真实 pyVideoTrans 云端合成、真实网易云上传、tests/live、真实 workspace rebuild --apply。网易云分页、同名时长匹配、状态不明不重传及 receipt 写入由更新后的专用 agent 负责，未用真实远端服务验收。

未扩大范围：没有统一 ASR、没有修改 learning-learn/thread.yaml、没有建立个人 Git 仓库或 submodule、没有移动现有 Media/Vault、没有提交或推送。

现场保护：执行前已有的 CDP/小鹅下载修改与新增文件均保留；没有 reset 或覆盖。现有 migration quarantine、会话和历史 package 未删除。
