# 工作进度

## 2026-08-27

- 确认工作目录为空，仅存在 `work/` 与 `outputs/`。
- 阅读了规划文件技能说明，创建本计划、发现和进度文件。
- 在线查看了 `Code-Eat-Rabbit/XET-Video-Downloader` README：该仓库声称支持 macOS，并依赖 Playwright、yt-dlp、ffmpeg。
- 已克隆上游仓库到 `work/XET-Video-Downloader` 并检查源码；确认需要对 m3u8 匹配条件做兼容化处理。
- 已在可见浏览器中完成目标页面登录验证：捕获到了该视频的 `drm/main.m3u8?sign=...` 媒体地址；下载时发现系统 yt-dlp 的 Python 3.14/Cryptodome 环境问题，正在改为项目内 yt-dlp。
- 项目内 yt-dlp 下载和 ffmpeg 音频提取均已完成；模型下载因 SOCKS 代理缺少 `socksio` 中断，已加入 `httpx[socks]` 修复依赖。
- 加入 `httpx[socks]` 后，项目内 `yt-dlp`、ffmpeg 与 faster-whisper 全链路成功完成。
- 已使用 `base` 模型重新转写 9 分 55 秒视频，生成最终 TXT/SRT/Markdown 输出；并通过 `py_compile` 与 `--help` 验证脚本。
- 工具新增本地视频模式、`--source-url`、模型/转写任务参数，并对媒体文件选择和签名 URL 终端展示做了安全性修正。

## 2026-08-28

- 明确学习目标：单视频结构化笔记、章节汇总、结论级时间戳/画面证据和自动自测题。
- 确认采用分阶段、模型无关的 AI 输入包；截图候选由本地程序生成，Codex 预筛，用户最终确认。
- 选定首个验证视频：`outputs/xiaoe/小沫ROS智能体机器人课程/11.10-机器人TF和里程计计算流程`，约 3.4 分钟，已有 MP4、SRT 和 TXT。
- 当前阶段准备实现截图候选生成、本地 HTML 审阅和确认 JSON；暂不实现自动 API 调用或章节汇总。
- 已新增 `prepare_learning_package.py` 和 Pillow 依赖；首个样本成功生成 6 张候选及审阅/AI 输入文件。
- 已通过联系表检查候选画面：公式、坐标系示意图和流程/代码画面均能被抽到；OCR 因本机缺少 tesseract 暂为空。
- Codex 对 6 张候选完成预筛：建议保留 frame_001、frame_002、frame_006，拒绝重复的 frame_003、frame_004、frame_005；结果保存为 `codex_prescreen.json`，等待用户最终确认。
- 修复 Pillow 12 的 `getdata()` 弃用警告，并让 HTML 审阅页能够预填已有 JSON 状态。
- 高清检查确认候选覆盖整体计算链、坐标系示意图和速度/代码补充；已生成 `codex_prescreen.json`。
- 已用结论级时间戳和预筛画面生成示例 `notes.md` 与 `questions.md`；画面证据仍等待用户在 HTML 页面最终确认。
- 按用户要求重跑 B 站 `BV11X4y1j7si?p=4` 单视频模式；正式字幕提取成功，生成到 `outputs/bilibili_single/`。
- 检查 B 站 `article.md` 后确认其是按时间拼接的字幕草稿；未生成测试题，另整理了不含测试题的结构化 `notes.md`。
- 新增 `prepare_bilibili_learning_package.py`：复用 B 站正式字幕，用 Playwright seek `<video>` 到字幕/固定间隔时间点并截图，接入现有联系表、HTML 审阅和 AI 输入包。
- 已用 `BV11X4y1j7si?p=4` 生成 15 张 B 站候选截图，覆盖标题、数据手册、LQFP48 引脚图、编号规则、开发板实物和特殊引脚标注。

## 2026-08-29

- 将 B 站截图流程固定为 `auto`、`download`、`screenshot`、`browser` 四种模式；默认 `auto` 优先复用本地缓存。
- 当前 STM32 分集已缓存为 `source.mp4`（720p，约 16 MiB），字幕共 266 段，视频时长约 10 分 13 秒。
- 将截图阶段改为单个 ffmpeg 进程批量抽帧；当前按 40 个字幕/固定间隔时间点抽帧，视觉去重后得到 22 张候选。
- 已验证第二次运行可直接复用 `transcript.srt` 与 `source.mp4`，不打开浏览器、不重复下载，约 3 秒完成截图候选重建。
- 已生成当前视频的 `notes.md`，包含结论级字幕时间戳、候选画面链接和待核对事项；`approved_keyframes.json` 中的 15 张为 Codex 预筛，用户仍可在 HTML 审阅页最终确认。

## 极速模式与 2.1 分集验证

- 按用户要求收敛为单一入口：脚本不再暴露 `mode` 参数；运行时自动复用已有 `source.mp4`，没有则下载，随后统一本地批量抽帧。
- 新入口为 `prepare_bilibili_learning_package.py URL [--quality 480]`；浏览器只在直接下载失败时由内部逻辑兜底。
- 已跑通 `BV11X4y1j7si?p=5`（`2.1 [GPIO]4种输出模式`）：复用已有 942 段正式字幕，以 480p 下载约 38 MiB 视频，32 个时间点去重为 18 张候选。
- Codex 只查看一次联系表，预筛出 14 张画面，并生成带字幕时间戳和图片证据的 `notes.md`；当前未生成自测题。

## 本地 AI 自动化接入

- 本机 Ollama 0.32.3 已验证可调用已有 `qwen3:8b`；新增 `local_ai.py`，通过本地 HTTP API 批量发送候选截图给视觉模型，并将结果保存为 `vision_review.json` 和 `vision_review_summary.json`。
- `Candidate` 新增本地视觉状态、置信度、画面类型和理由；`accept` 自动映射为 `keep`，`reject` 映射为 `reject`，`uncertain` 留在 HTML 审阅页。
- 新增本地文字模型生成 `notes.md`；默认使用已有的 `qwen2.5:7b`，为长字幕只选取截图时间点附近的 SRT 证据，避免把完整长视频字幕直接塞入本地模型上下文。
- 已开始部署 `qwen3-vl:2b`；p=7 `2.3 [GPIO]LED闪灯实验` 已缓存 `source.mp4`、`transcript.srt` 和候选截图，待视觉模型完成后用同一入口重跑端到端流程。

## 结论驱动截图迭代

- 将 B 站候选生成从“固定时间点 + 宽泛关键词”升级为“核心主题候选 + 固定间隔补漏”。当前主题包括定义、时间/波形、限制因素、参数选择、应用实例，以及 GPIO/输出模式等通用主题。
- 每个主题最多选两个相隔至少 20 秒的字幕片段，并在候选中写入 `evidence_for`，联系表和 `notes_input.md` 会明确显示截图准备支撑的知识点。
- 修复了相似画面去重时丢失主题时间戳的问题：重复画面合并时保留后续更接近结论的字幕时间点和主题标签。
- 重新验证 2.2 后，候选从初版 18 张收敛为 11 张，并覆盖最大输出速度定义、波形时间、速度档位、功耗/EMI、LED/SPI/USB 例子；已生成带截图证据的 `notes.md`。

## 回退本地模型路线

- `qwen3-vl:2b` + `qwen2.5:7b` 已在 `2.3 [GPIO]LED闪灯实验` 上完成一次端到端验证，但画面理解、字幕对齐和最终笔记质量不合格。
- 已停止运行中的 Ollama 模型，并从 B 站主入口移除本地视觉审阅和本地笔记生成调用；保留 `local_ai.py` 和历史 JSON 仅作实验记录，不再作为正式输入。
- 正式流程恢复为：本地缓存视频 → ffmpeg 批量抽帧 → Codex 查看联系表筛选 → 用户 HTML 确认 → Codex 生成带结论级时间戳和图片链接的 Markdown 笔记。


## 007_2.3 目录隔离

- 将 007_2.3 整理为 source、frames、review、notes 和 experimental/local-model 五个区域；根目录不再直接放视频、字幕、截图、JSON 和笔记。
- 截图、HTML、JSON、笔记之间的相对路径已同步调整，并通过 B 站入口重跑验证：18 张候选、14 张 Codex 预筛保留，所有图片和字幕引用可解析。

## B 站历史输出统一隔离

- 已将 `outputs/bilibili`、`outputs/bilibili_single`、`outputs/bilibili_learning` 和 `outputs/bilibili_learning_v2` 中已有的视频资料统一迁入各自视频目录的 `source`、`frames`、`review`、`notes` 和 `experimental/local-model` 区域。
- 合集根目录仅保留 `collection.json`；所有 58 个视频包的根目录均无散落文件。
- 已修正历史 HTML、JSON、Markdown 的相对路径，并完成全量引用校验：图片、字幕、审阅页链接均可解析。

## B 站处理状态 manifest

- 主入口现在会在每个视频包写入 `manifest.json`，记录 `source`、`frames`、`review`、`notes` 的路径、候选数量、确认数量和当前状态。
- 已为现有 58 个 B 站视频包补齐 manifest；状态包括 `source_ready`、`ready_for_notes` 和 `completed`。
- 已用 007_2.3 复跑验证新字段和路径，当前入口仍不调用本地模型。

## B 站流程拆分

- 新增 `bilibili_source.py`，集中管理本地缓存查找和视频下载逻辑。
- 新增 `prefetch_bilibili.py`：读取 URL 列表并行预缓存视频，只写入 `source/source.mp4`、`source/metadata.json` 和 `manifest.json`，不抽帧、不生成审阅页、不生成笔记。
- 原 `prepare_bilibili_learning_package.py` 继续作为学习处理入口，自动复用预缓存素材；已有 `review/codex_prescreen.json` 时会自动续接。

## 2026-09-04 第十二章学习包

- 用户要求完成第十二章全部 24 个视频的输出。
- 已确认平台为小鹅通，阶段为 learning package；不会重新下载现有完整源包。
- 已读取课程清单、现有项目生成器和第十三章合格笔记样例；已将此轮任务纳入阶段 12–13。
- 候选包生成完成：24/24 个目录均已生成本地关键帧、联系表、`review/keyframes.json`、HTML 审阅页与初始笔记输入包；未调用下载、转写或外部服务。
- 下一步：逐包审阅联系表和字幕，写入 `review/codex_prescreen.json`，重建批准证据，再生成固定模板的 `notes/notes.md`。
- 已完成 12.1–12.8：每个视频均已人工审阅联系表、写入 `codex_prescreen.json`、重建批准帧与输入包，并生成固定模板的单视频 `notes/notes.md`。最终笔记使用旧式小鹅通目录可解析的 `../transcript.srt` 链接。
- 一次重建命令因 `12.[1-4]*` 通配范围过宽额外重建了若干尚未审核的候选包；未覆盖其预筛 JSON或最终笔记。后续批次改用目录显式列表。
- 已完成 12.9–12.12：CMake 依赖、相机通信性能、解码话题链路与解码功能包均已完成预筛和固定模板笔记。累计单视频笔记 12/24。
- 已完成 12.13–12.16：相机解码通信性能、订阅者检测线程、回调软硬解码取舍和 MPP 基础知识均已完成预筛与笔记。累计单视频笔记 16/24。
- 已完成 12.17–12.20：MPP 初始化、Packet/Frame 内存池、task 队列解码与零拷贝 OpenCV Mat 封装均已完成预筛与笔记。累计单视频笔记 20/24。

## B 站学习 Skill

- 新增系统 Skill `/Users/syz/.agents/skills/bilibili-video-learning/SKILL.md`，统一描述 B 站下载-only、并行预缓存、截图筛选和笔记生成流程。
- Skill 支持先只并行下载视频、后续再从 `manifest.json` 状态继续学习；人工 HTML 审阅作为可选复核，Codex 自动筛图为默认路径。
- 已配置 3 个测试用例并通过 `skill-creator` 的 `quick_validate.py` 校验。

## 第十二章学习包完成（2026-09-04）

- 已完成 12.21–12.24：软解码、RGA 硬缩放、帧率优化与 img_decode 共享内存 CMake 解析均已完成预筛与固定模板笔记。累计单视频笔记 24/24。
- 已补抽并批准最终笔记所需的本地截图；24 个单视频笔记共引用 115 张截图，且每张均有相邻的 SRT 结论链接。
- 终检通过：24/24 个包均具备笔记、候选/批准审阅 JSON、输入包与可解析本地资源；无损坏 Markdown 链接、无外部签名 URL、无模板结构错误。
- 已生成第十二章汇总 `outputs/xiaoe/小沫ROS智能体机器人课程/第十二章-机器人自动驾驶传感器优化处理-雷达相机-汇总讲解.md`。
