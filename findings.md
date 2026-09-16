# 调研与发现

## 本地视觉模型验证结论

- 曾验证 Ollama 可以在本机接收候选截图，但小模型对代码/PPT/硬件画面的识别和字幕对齐不稳定，不能作为正式筛选或笔记生成路径。
- 已停止默认入口中的本地视觉审阅和本地笔记生成；Ollama 模型不再参与 B 站处理。
- 当前正式路径仍由规则生成时间点、ffmpeg 批量抽帧，Codex 查看联系表后筛选截图并生成最终 Markdown，用户通过 HTML 页面做最终确认。

## XET-Video-Downloader

- GitHub 仓库 README 声明支持 macOS/Linux/Windows，要求 Python 3.10+、uv，并使用 Playwright 与 yt-dlp 捕获/下载 m3u8。
- README 的捕获逻辑说明：首次运行显示浏览器完成登录，登录状态保存在 `browser_session`；视频输出到 `downloads`。
- 目标 URL 使用 `h5.xet.pomoho.com`，属于小鹅通/小鹅通相关页面形态，需实际运行验证当前媒体请求是否仍符合仓库的匹配规则。

## 实现方向

- 将下载器作为一个可调用的本地步骤，尽量复用其 `VideoDownloader`，必要时用更宽松的 m3u8 捕获补丁。
- 转写使用 faster-whisper 本地模型，支持 Apple Silicon 上的 CPU/int8；不需要 OpenAI API key。
- 输出至少包括音频、纯文本转写、带时间戳 SRT、结构化 Markdown 文章草稿。

## 本机检查

- 当前工作目录为空；已将上游仓库克隆到 `work/XET-Video-Downloader` 供参考。
- 上游 `main.py` 当前只接受 URL 同时包含 `.m3u8`、`sign=`、`v.f421220` 的请求，且依赖 Python >=3.10。
- 本机已有 `/opt/homebrew/bin/ffmpeg`，但当前 `python3` 是 3.9.6，尚未检测到 `uv` 和 `yt-dlp`。

## 第一次目标视频实测

- 用户完成浏览器登录后，目标视频成功打开并播放，工具捕获到了 `https://v-vod-k.xiaoeknow.com/.../drm/main.m3u8?sign=...`。
- 该地址没有 `v.f421220`，但包含 `.m3u8` 和签名参数，验证了兼容化捕获逻辑的必要性。
- Homebrew 的 `yt-dlp` 启动时使用 Python 3.14 的系统包装环境，并在 Cryptodome 动态库加载处异常/卡住；工具已改为把 yt-dlp 纳入项目虚拟环境并通过 `python -m yt_dlp` 调用。
- 项目内 yt-dlp 已成功下载目标视频（约 24.3 MiB、时长约 9 分 56 秒、1280x720）并用 ffmpeg 提取出 WAV。
- 首次下载 Whisper 模型时，当前环境通过 SOCKS 代理访问模型仓库，但虚拟环境缺少 `socksio`；已将 `httpx[socks]` 加入依赖，后续从现有本地视频继续。

## 学习资料 V1 设计

- 验收链路：已有 `video.mp4` + `transcript.srt`/`transcript.txt` → 混合截图候选 → HTML 审阅 → `approved_keyframes.json` → AI 输入包。
- 候选来源：固定间隔抽帧、字幕关键词锚点附近抽帧、ffmpeg 场景变化检测；按时间窗口和感知缩略图相似度去重。
- Codex 审图的 token 控制：本地先压缩到每个视频约 20~50 个候选，联系表按 12~20 张分批，只有保留/不确定截图再查看高清图。
- 首个样本 `11.10-机器人TF和里程计计算流程` 适合验证，因为转写包含坐标系、编码器、里程计和 TF 流程，预期画面信息比纯理论视频更有价值。

## 首次截图候选验证

- `prepare_learning_package.py` 已在首个样本上成功生成 6 张候选、`contact_sheet.jpg`、`contact_sheet.html`、`keyframes.json`、`approved_keyframes.json` 和 `notes_input.md`。
- 6 张候选覆盖公式页、坐标系示意图和末尾流程/代码页；联系表中仍能看出部分相似画面，后续需要按“知识点覆盖”而不是只按时间覆盖来优化。
- 本机未安装 tesseract，因此候选的 OCR 字段为空、`visual_hint` 为 `unknown`；这不阻塞截图和人工审阅，但后续可提供可选 OCR 安装说明或视觉模型预筛。
- Pillow 12 对 `Image.getdata()` 发出弃用警告，功能成功但应在下一次修改中替换为新 API 或固定 Pillow 版本兼容写法。

## B 站单视频验证

- `convert_voice_to_article.py --bilibili` 已成功处理 `BV11X4y1j7si?p=4`，输出到 `outputs/bilibili_single/1.2 [准备]STM32的引脚分布/`。
- B 站正式字幕共 266 个 SRT 段，专有名词识别质量明显好于小鹅通本地 Whisper，但 `article.md` 仍是按时间分组的字幕草稿。
- 已额外生成结构化 `notes.md`，覆盖封装判断、引脚编号、VDD/VSS、NRST、VBAT、BOOT0 和 GPIO 命名，并保留时间戳证据；没有生成测试题。
- 当前 B 站字幕模式不下载视频，所以本次没有画面证据；若要截图，需要后续增加 B 站视频下载或浏览器画面捕获步骤。

## B 站本地缓存与批量截图验证

- B 站页面不一定主动发起可捕获的 m3u8 请求；对当前公开视频，直接使用 yt-dlp 页面 URL 能稳定选择最高 720p 的视频流和音频流并合并为 `source.mp4`。
- `auto` 首次运行会生成字幕和 `source.mp4`；再次运行通过 `metadata.json` 中的 `source_url` 找到视频目录，复用本地字幕和视频。
- 截图阶段使用一个 ffmpeg 进程顺序解码，通过多个时间窗 `select` 输出候选帧，再依据帧率和输出 PTS 回配到字幕时间点；当前 10 分钟视频的 40 个时间点约 3 秒完成。
- 相似度去重仍然会合并连续时间点的相同画面，这是预期行为；`keyframes.json` 保留最终候选的实际字幕/抽帧时间点。
- 当前分集最终生成 `outputs/bilibili/1.2 [准备]STM32的引脚分布/`：`source.mp4`、`transcript.srt`、22 张候选、HTML 审阅页、15 张 Codex 预筛保留/备注画面和带截图证据的 `notes.md`。

## 极速模式验证

- 为减少使用复杂度，B 站脚本只保留一个固定入口；`mode` 不再作为用户参数，缓存复用、下载、批量抽帧和审阅包生成由脚本自动完成。
- `2.1 [GPIO]4种输出模式` 已使用 `--quality 480` 成功下载并处理；低分辨率足以识别本视频的 PPT、电路图、代码和硬件画面。
- 32 分钟视频只产生 18 张去重候选，Codex 通过一次联系表审阅完成预筛；文档以字幕为主线，仅在关键知识点插入少量截图。

## 结论驱动截图验证

- 仅用 `GPIO`、`引脚` 等宽泛关键词会让主题标签泛化，不能准确表达截图用途；加入“定义、限制因素、档位选择、应用实例、上升/下降/保持时间”等结论类型后，候选语义更稳定。
- 当前 2.2 验证把 31 个候选时间点去重为 11 张，联系表已经形成从定义到应用的证据链：最大输出速度、波形时间、速度档位、功耗/EMI、LED/SPI/USB。
- `evidence_for` 目前是规则生成的“证据主题候选”，Codex 仍需依据联系表确认画面是否真的支撑该主题；它不是对视频内容的最终事实判断。

## 第十二章笔记任务（2026-09-04）

- 小鹅通课程 `第十二章-机器人/自动驾驶-传感器优化处理-雷达-相机` 清单有 24 个视频，源包均完整（MP4、SRT、TXT、article、metadata）。
- 任务开始前第十二章没有任何 `notes/notes.md`、`review/codex_prescreen.json` 或 `notes/notes_input.md`。
- 既有第十三、十四章笔记采用目标目录内的 `source/`、`frames/`、`review/`、`notes/` 布局；第十二章源包仍是旧的目录平铺布局。生成器可直接用视频目录作为输出目录，且会在目录内补建这些标准子目录。
- 既有 `13.1-什么是机器人URDF文件` 笔记说明了可复用的交付规范：固定模板、中文解释、结论级 SRT 链接、相邻截图和不确定项声明。
- 已对第十二章全部 24 个视频完成本地候选包生成：每个目录现在都有 `frames/keyframes/`、`frames/contact_sheet.jpg`、`review/keyframes.json`、`review/contact_sheet.html` 与初始 `notes/notes_input.md`。生成时禁用不可用的 OCR，未重新下载或转写。
- 生成器在旧式小鹅通目录中会在输入包示例里写 `../source/transcript.srt`，而实际字幕仍在视频目录根部。因此最终 `notes.md` 必须使用可解析的 `../transcript.srt` 链接；重跑输入包后也需要校正该提示中的示例路径。

## 第十二章最终交付校验

- 第十二章的旧式平铺目录与后续章节的 `source/` 布局不同：最终笔记和输入包都应链接 `../transcript.srt`，而非 `../source/transcript.srt`。
- 最终审阅以笔记实际引用的截图为批准集合；候选包未持久化的画面已从本地 MP4 按相邻字幕结论时间补抽，并登记为人工视觉复核证据。
- 24 个单视频笔记共引用 115 张本地截图；批准清单、笔记图片和字幕链接已逐包比对一致。
