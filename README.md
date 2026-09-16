# video-extract

`video-extract` 是授权视频学习 package 的稳定 interface。项目内 validator 是状态的唯一事实源；旧 Python 脚本保留为兼容入口，新调用从统一 CLI 开始：

```bash
uv run video-extract --help
uv run video-extract doctor --json
uv run video-extract status /path/to/package --json
uv run video-extract verify /path/to/package --json
```

公开工作流是媒体请求（`--media`）和学习笔记目标 `notes_zh`。`podcast_zh` 只保留给 schema v1–v4 兼容。先用只读规划，再执行：

```bash
video-extract plan SOURCE --media audio --language zh-CN [--output EXISTING_PACKAGE] --json
video-extract ensure SOURCE --goal notes_zh --keep-video auto --output PACKAGE --json
video-extract verify PACKAGE --goal notes_zh --json
```

中文音频默认是自然节奏的“中文听觉版”，不是与原视频时间轴同步的配音。原生中文轨优先；没有原生中文轨时只支持英文源，先以 `--language original` 提取 `media/audio.source.m4a`，再由顶层 `extract-media` 工作流直接执行 pyVideoTrans 的 `cli.py --task podcast`。pyVideoTrans 独立拥有 `listening/zh-CN/`，video-extract 不采用、复制或验证其中产物。非英文或未知源明确不支持，不触发云调用。

`--keep-video auto|yes|no` 仅控制视频保留；`auto` 会复用已有/本地视频，否则使用 480p 工作代理并在目标验证成功后清理。完整 schema v4、目标验证、AI 暂停与恢复约束见 [package contract](docs/package-contract.md)。旧低层命令和 schema 1–3 继续兼容。

`--audio-quality standard|high` 分别编码为 128 kbps 与 192 kbps AAC。B 站和小鹅通优先使用 yt-dlp 清单；受限但已授权的媒体会回退到现有持久浏览器会话，由用户完成登录和播放后再捕获媒体流。

音轨选择不依赖 yt-dlp 返回顺序：优先原始语言，再按默认语言/语言偏好回退，并在目标语言内选择符合质量档位的最高码率独立音轨。`plan` 只读取公开元数据，绝不会打开持久浏览器；浏览器授权回退只由 `ensure` 触发。

## 状态与并发

Collection 使用 `catalog_ready` 与显式请求的 `summary_complete`；item 依次通过 `media_ready → transcript_ready → candidates_ready → evidence_selected → notes_complete`。相同输入和参数的已验证阶段可复用。浏览器捕获按持久会话串行；下载、ffmpeg、Whisper 分别控制并行度，Whisper 模型在每个 worker 内复用。

历史 schema 1/2 manifest 无需批量迁移；`status/verify` 始终检查真实 artifacts，不信任旧状态名。

## 兼容入口

下列独立脚本仍可用于既有流程；新编排不需要记忆这些文件名。

## macOS 安装

Apple Silicon（包括 M 系列）可使用。先安装系统依赖：

```bash
brew install uv ffmpeg
uv sync
uv run playwright install chromium
```

首次运行必须显示浏览器，以便你自己登录并播放视频：

```bash
uv run python convert_voice_to_article.py \
  'https://apptfnwzszx1898.h5.xet.pomoho.com/p/course/video/v_69242b68e4b0694c5b565eb5?product_id=course_340AJfvK2AmN3slRk5g7sdHzed4&sub_course_id=&auto=true'
```

浏览器打开后，在页面中完成登录并让视频播放几秒。登录状态只保存在本地 `work/browser_session/`。程序不会要求账号、密码、Cookie 或 API key。

默认输出在 Obsidian Vault 的 `/Users/syz/code/obsidian_本地知识库/编程开发/网站视频转录/xiaoe/`，每个视频一个独立目录：

```text
/Users/syz/code/obsidian_本地知识库/编程开发/网站视频转录/xiaoe/
└── 小沫ROS智能体机器人课程/
    ├── course_catalog.json
    ├── 7.13-ROS-相机坐标系解析/
│   ├── 7.13-ROS-相机坐标系解析.mp4
│   ├── 7.13-ROS-相机坐标系解析.wav
│   ├── transcript.txt
│   ├── transcript.srt
│   ├── article.md
│   └── metadata.json
    └── 其他视频标题/
```

每个视频目录包含：

- 视频文件和 16kHz 单声道 WAV 音频
- `transcript.txt`：纯文本
- `transcript.srt`：带时间戳字幕
- `article.md`：按时间段整理的文章草稿
- `metadata.json` 与 `captured_media.json`：处理元数据和捕获结果

其中 `captured_media.json` 包含临时签名地址，只用于本地排错，不要上传或分享。

## 扫描课程目录

课程主页可以先扫描并保存目录，不需要手工复制每个视频地址：

```bash
uv run python scan_course.py \
  'https://apptfnwzszx1898.h5.xet.pomoho.com/p/course/ecourse/course_340AJfvK2AmN3slRk5g7sdHzed4'
```

清单保存在对应的 `/Users/syz/code/obsidian_本地知识库/编程开发/网站视频转录/xiaoe/小沫ROS智能体机器人课程/course_catalog.json`，包含课程名称、章节、小节标题、视频页面 URL、时长、资源类型和学习状态。

首次没有登录时使用 `--visible`，在弹出的浏览器中由你自己登录：

```bash
uv run python scan_course.py '课程主页URL' --visible --wait-seconds 30
```

## 批量处理章节

扫描清单后，可以按章节批量下载、提取音频和本地转写。每个视频会单独建立标题目录，并用完整文件检查跳过已完成项目：

```bash
uv run python batch_process.py --section '第七章-机器人传感器接入'
```

当前批处理脚本固定处理第七章；`--workers 2` 可调整下载/转写并行数，`--limit N` 可先试跑前 N 个。进度保存在对应课程目录。

## 本地翻译

默认是中文转写：

```bash
uv run python convert_voice_to_article.py URL --task transcribe --language zh
```

如果希望 Whisper 本地直接翻译成英文：

```bash
uv run python convert_voice_to_article.py URL --task translate --language zh
```

首次使用某个模型会从模型仓库下载模型文件；之后会复用本地缓存。`small` 是默认折中选择，也可换成 `base`、`medium` 或本地模型目录。

## B 站合集字幕

使用独立浏览器会话提取整个 B 站合集的正式字幕：

```bash
uv run python bilibili_collection.py \
  'https://www.bilibili.com/video/BV11X4y1j7si?p=4' \
  --wait-seconds 12
```

输出目录为 `/Users/syz/code/obsidian_本地知识库/编程开发/网站视频转录/bilibili/合集标题__BV号/`，每个分集一个带序号的目录，并生成 `collection.json` 记录每集状态。首次运行使用可见浏览器扫码登录；登录状态仅保存在 `work/bilibili_browser_session/`。

## B 站字幕（独立浏览器）

不想授权读取普通 Chrome 的钥匙串时，可以使用独立的 Playwright 浏览器：

```bash
uv run python convert_voice_to_article.py --bilibili \
  'https://www.bilibili.com/video/BV11X4y1j7si?p=4' \
  --wait-seconds 60
```

程序会打开独立窗口。请你在窗口中扫码登录 B 站并播放视频，登录状态只保存到 `work/bilibili_browser_session/`，不会读取普通 Chrome Cookie 或 macOS 钥匙串。若该视频没有正式字幕轨道，程序会明确提示；弹幕不算字幕。

## 已有本地视频

无需重新捕获或下载：

```bash
uv run python convert_voice_to_article.py --video /path/to/video.mp4
```

如需在本地视频模式保留原始页面地址，可加 `--source-url URL`。

注意：本工具只用于下载和处理你本人已购买或有权访问的内容，不用于绕过 DRM 或访问控制。

## 为已有视频生成截图候选和 AI 学习输入包

可以跳过下载/转写，直接对已有视频目录生成固定抽帧、字幕锚点和场景变化候选：

```bash
uv run python prepare_learning_package.py \
  '/Users/syz/code/obsidian_本地知识库/编程开发/网站视频转录/xiaoe/小沫ROS智能体机器人课程/11.10-机器人TF和里程计计算流程/11.10-机器人TF和里程计计算流程.mp4'
```

输出包括（输出目录按用途隔离）：

- `frames/keyframes/`、`frames/contact_sheet.jpg`：候选截图和联系表
- `review/contact_sheet.html`、`review/*.json`、`review/codex_review_prompt.md`：审阅页、候选/确认结果和 Codex 预筛记录
- `notes/notes_input.md`、`notes/notes.md`：笔记输入包和最终笔记
- `manifest.json`：当前处理状态、素材路径和候选/确认数量，供后续流程快速续接
- `source/`：视频、字幕、原始转写和元数据

如已有 Codex 预筛结果，可重新导入并预填审阅页：

```bash
uv run python prepare_learning_package.py VIDEO_PATH \
  --review-json codex_prescreen.json
```

截图候选只是机器建议；最终进入学习笔记的画面仍需要人工在 HTML 审阅页确认。

### B 站视频按字幕定位截图

如果只想先批量下载视频、不抽帧也不生成笔记，可以使用独立的预缓存入口。它只写入 `source/source.mp4`、`source/metadata.json` 和 `manifest.json`，默认并行 3 个任务：

```bash
uv run python prefetch_bilibili.py bilibili_urls.txt --workers 3 --quality 480
```

`bilibili_urls.txt` 每行一个视频 URL；下载完成后，后续运行下面的学习入口会自动复用这些本地视频。

B 站正式字幕也可以作为截图时间轴。默认流程会先缓存一份最高 480p 的本地视频，后续直接复用 `source.mp4`，用单个 ffmpeg 进程批量抽帧，再生成联系表和审阅页：

```bash
uv run python prepare_bilibili_learning_package.py \
  'https://www.bilibili.com/video/BV11X4y1j7si?p=4'
```

这是唯一需要记忆的入口：有 `source.mp4` 就复用，没有就自动下载；下载后始终使用本地视频批量抽帧，不再让用户选择模式。默认下载最高 480p；需要更清晰画面时可使用 `--quality 720`。若直接 yt-dlp 无法访问受限视频，程序内部才会切换到浏览器捕获媒体地址；首次使用可见浏览器并手动登录，已登录后可加 `--headless`。

正式输出目录固定为 `/Users/syz/code/obsidian_本地知识库/编程开发/网站视频转录/bilibili/视频标题/`，内部按 `source/`、`frames/`、`review/`、`notes/` 分组；本地模型实验产物（如果历史上存在）放在 `experimental/local-model/`，不参与正式流程。仓库中历史 `outputs/bilibili*` 目录不再作为新流程入口。

### Codex 筛选截图并生成最终笔记

当前正式路线不调用本地视觉模型或本地文字模型：

1. 运行上面的 B 站入口，脚本复用/下载视频并批量抽取候选截图。
2. Codex 查看 `frames/contact_sheet.jpg`，结合 `review/keyframes.json` 和 `source/transcript.srt`，筛掉重复、无信息或无法支撑结论的画面，写出 `review/codex_prescreen.json`。这是默认判断步骤，人工 HTML 审阅暂时只作为可选复核。
3. 再次运行同一个 B 站入口；脚本会自动发现 `review/codex_prescreen.json`，重建 `review/approved_keyframes.json` 和 `notes/notes_input.md`，不需要再次传 `--review-json`。
   此时 `manifest.json` 状态会记录为 `ready_for_notes`，并给出下一步动作 `codex_generate_notes`；尚未筛图时为 `ready_for_review`。

4. Codex 读取确认后的截图、字幕和 `notes/notes_input.md`，生成最终的 `notes/notes.md`。笔记使用结论级时间戳，并用 `![说明](../frames/keyframes/candidate_001.jpg)` 直接嵌入图片，不自动生成自测题。
本地 HTML 审阅页保留为可选复核入口；Codex 预筛是当前默认的截图判断方式。`local_ai.py` 仅作为已停止的实验性代码保留，不会被默认入口调用，也不需要启动 Ollama。
